from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Any, Optional
import httpx

app = FastAPI(title="Sorare Projections API", version="0.4.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SORARE_GRAPHQL = "https://api.sorare.com/graphql"

QUERY = """
query PlayersQuery($slugs: [String!]!) {
  players(slugs: $slugs) {
    slug
    displayName
    anyPositions
    activeClub { name slug }
    averageScore(type: LAST_FIVE_SO5_AVERAGE_SCORE)
    averageScore10: averageScore(type: LAST_TEN_PLAYED_SO5_AVERAGE_SCORE)
    averageScore40: averageScore(type: LAST_FORTY_SO5_AVERAGE_SCORE)
    nextClassicFixtureProjectedScore
    nextClassicFixtureProjectedGrade {
      grade
      reliabilityBasisPoints
      score
    }
    nextGame(so5FixtureEligible: true) {
      ... on Game {
        id
        date
        statusTyped
        homeTeam { name slug }
        awayTeam { name slug }
      }
    }
  }
}
"""


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def normalize_position(raw: Any) -> str:
    if isinstance(raw, list) and raw:
        raw = raw[0]
    p = str(raw or "").upper()
    if "GOAL" in p:
        return "GK"
    if "DEFEND" in p:
        return "DEF"
    if "FORWARD" in p or "STRIKER" in p:
        return "ATT"
    return "MID"


def estimate_metrics(ps: float, position: str, home: Optional[bool]) -> dict:
    p = float(ps)
    home_boost = 2.0 if home is True else (-1.0 if home is False else 0.0)
    p_adj = p + home_boost

    win_pct = clamp(round(32 + (p_adj - 35) * 1.15), 8, 82)

    xg = None
    xa = None
    cs_pct = None

    if position in ("GK", "DEF"):
        cs_pct = clamp(round(12 + (p_adj - 30) * 0.95), 5, 68)
        xg = round(max(0.01, p_adj * 0.004), 2)
        xa = round(max(0.01, p_adj * 0.006), 2)
    elif position == "ATT":
        xg = round(max(0.05, p_adj * 0.014), 2)
        xa = round(max(0.03, p_adj * 0.009), 2)
    else:
        xg = round(max(0.03, p_adj * 0.009), 2)
        xa = round(max(0.04, p_adj * 0.012), 2)

    return {
        "winPct": win_pct,
        "xg": xg,
        "xa": xa,
        "csPct": cs_pct,
        "model": "form-v1",
    }


def pack_player(player: dict) -> dict:
    position = normalize_position(player.get("anyPositions"))
    club = (player.get("activeClub") or {}).get("name")

    last5 = player.get("averageScore")
    last10 = player.get("averageScore10")
    last40 = player.get("averageScore40")
    last5 = round(float(last5), 1) if isinstance(last5, (int, float)) else None
    last10 = round(float(last10), 1) if isinstance(last10, (int, float)) else None
    last40 = round(float(last40), 1) if isinstance(last40, (int, float)) else None

    next_game = player.get("nextGame")
    has_fixture = next_game is not None

    fixture = None
    home = None
    if has_fixture and isinstance(next_game, dict):
        home_t = next_game.get("homeTeam") or {}
        away_t = next_game.get("awayTeam") or {}
        home = bool(club and home_t.get("name") == club)
        fixture = {
            "kickoff": next_game.get("date"),
            "homeTeam": home_t.get("name"),
            "awayTeam": away_t.get("name"),
            "home": home,
            "status": next_game.get("statusTyped"),
        }

    grade_obj = player.get("nextClassicFixtureProjectedGrade")
    official_ps = player.get("nextClassicFixtureProjectedScore")
    grade = None
    reliability = None
    if grade_obj:
        grade = grade_obj.get("grade")
        if isinstance(grade_obj.get("reliabilityBasisPoints"), int):
            reliability = round(grade_obj["reliabilityBasisPoints"] / 100, 1)
        if grade_obj.get("score") is not None:
            official_ps = grade_obj.get("score")

    ps = None
    ps_source = None
    if has_fixture and isinstance(official_ps, (int, float)) and official_ps > 0:
        ps = round(float(official_ps), 1)
        ps_source = "sorare-projection"
    elif last5 is not None:
        ps = last5
        ps_source = "l5-form"
    elif last10 is not None:
        ps = last10
        ps_source = "l10-form"

    estimates = None
    if has_fixture and ps is not None:
        estimates = estimate_metrics(ps, position, home)

    sorare_projection = None
    if has_fixture and ps is not None and ps_source == "sorare-projection":
        sorare_projection = {
            "score": ps,
            "grade": grade,
            "reliabilityPct": reliability,
        }

    return {
        "slug": player.get("slug"),
        "displayName": player.get("displayName"),
        "position": position,
        "club": club,
        "form": {"last5": last5, "last10": last10, "last40": last40},
        "ps": ps,
        "psSource": ps_source,
        "hasUpcomingFixture": has_fixture,
        "fixture": fixture,
        "sorareProjection": sorare_projection,
        "estimates": estimates,
        "message": None if has_fixture else "No upcoming fixture",
        "source": "sorare+form-model",
    }


async def fetch_players(slugs: List[str]) -> dict:
    async with httpx.AsyncClient(timeout=25.0) as client:
        r = await client.post(
            SORARE_GRAPHQL,
            json={"query": QUERY, "variables": {"slugs": slugs}},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (compatible; SorareProjections/0.4)",
            },
        )
        body = r.json()
        return {
            "status": r.status_code,
            "errors": body.get("errors"),
            "players": ((body.get("data") or {}).get("players") or []),
        }


@app.get("/")
def root():
    return {"status": "ok", "message": "Sorare Projections API", "version": "0.4.0"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/project")
async def project(slugs: str = Query(...)):
    slug_list = [s.strip() for s in slugs.split(",") if s.strip()][:15]
    if not slug_list:
        return {"ok": False, "error": "no slugs", "data": {}}

    fetched = await fetch_players(slug_list)
    by_slug = {p["slug"]: p for p in fetched["players"] if p and p.get("slug")}

    results = {}
    for slug in slug_list:
        p = by_slug.get(slug)
        if not p:
            results[slug] = {
                "slug": slug,
                "error": "player_not_found",
                "hasUpcomingFixture": False,
                "message": "Player not found",
                "ps": None,
                "estimates": None,
                "_debug": {
                    "graphqlStatus": fetched["status"],
                    "graphqlErrors": fetched["errors"],
                },
            }
            continue
        results[slug] = pack_player(p)

    return {
        "ok": True,
        "version": "0.4.0",
        "data": results,
        "_meta": {
            "graphqlStatus": fetched["status"],
            "graphqlErrors": fetched["errors"],
        },
    }


@app.get("/debug")
async def debug(slug: str):
    fetched = await fetch_players([slug])
    return {
        "status": fetched["status"],
        "errors": fetched["errors"],
        "players": fetched["players"],
    }
