from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Any, Optional
import httpx

app = FastAPI(title="Sorare Projections API", version="0.3.3")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SORARE_GRAPHQL = "https://api.sorare.com/graphql"

# NOTE: allPlayerGameScores is NOT allowed inside players(slugs:) list queries.
# Use averageScore + nextGame + nextClassicFixtureProjected* only.
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


def pack_player(player: dict) -> dict:
    position = normalize_position(player.get("anyPositions"))
    club = (player.get("activeClub") or {}).get("name")

    last5 = player.get("averageScore")
    last10 = player.get("averageScore10")
    last40 = player.get("averageScore40")
    if isinstance(last5, (int, float)):
        last5 = round(last5, 1)
    else:
        last5 = None
    if isinstance(last10, (int, float)):
        last10 = round(last10, 1)
    else:
        last10 = None
    if isinstance(last40, (int, float)):
        last40 = round(last40, 1)
    else:
        last40 = None

    next_game = player.get("nextGame")
    has_fixture = next_game is not None

    fixture = None
    if has_fixture and isinstance(next_game, dict):
        home = next_game.get("homeTeam") or {}
        away = next_game.get("awayTeam") or {}
        fixture = {
            "kickoff": next_game.get("date"),
            "homeTeam": home.get("name"),
            "awayTeam": away.get("name"),
            "home": bool(club and home.get("name") == club),
            "status": next_game.get("statusTyped"),
        }

    sorare_projection = None
    if has_fixture:
        grade_obj = player.get("nextClassicFixtureProjectedGrade")
        ps = player.get("nextClassicFixtureProjectedScore")
        if grade_obj or (isinstance(ps, (int, float)) and ps > 0):
            sorare_projection = {
                "score": (
                    grade_obj.get("score")
                    if grade_obj and grade_obj.get("score") is not None
                    else ps
                ),
                "grade": (grade_obj or {}).get("grade"),
                "reliabilityPct": (
                    round(grade_obj["reliabilityBasisPoints"] / 100, 1)
                    if grade_obj
                    and isinstance(grade_obj.get("reliabilityBasisPoints"), int)
                    else None
                ),
            }

    return {
        "slug": player.get("slug"),
        "displayName": player.get("displayName"),
        "position": position,
        "club": club,
        "form": {
            "last5": last5,
            "last10": last10,
            "last40": last40,
        },
        "hasUpcomingFixture": has_fixture,
        "fixture": fixture,
        "sorareProjection": sorare_projection if has_fixture else None,
        "message": None if has_fixture else "No upcoming fixture",
        "source": "sorare-official",
    }


async def fetch_players(slugs: List[str]) -> dict:
    async with httpx.AsyncClient(timeout=25.0) as client:
        r = await client.post(
            SORARE_GRAPHQL,
            json={"query": QUERY, "variables": {"slugs": slugs}},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (compatible; SorareProjections/0.3)",
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
    return {
        "status": "ok",
        "message": "Sorare Projections API is running",
        "version": "0.3.3",
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/project")
async def project(
    slugs: str = Query(..., description="Comma-separated player slugs"),
):
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
                "form": None,
                "sorareProjection": None,
                "_debug": {
                    "graphqlStatus": fetched["status"],
                    "graphqlErrors": fetched["errors"],
                },
            }
            continue
        results[slug] = pack_player(p)

    return {
        "ok": True,
        "version": "0.3.3",
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
