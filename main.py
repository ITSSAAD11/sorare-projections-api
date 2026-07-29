from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Any, Optional
import httpx

app = FastAPI(title="Sorare Projections API", version="0.3.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SORARE_GRAPHQL = "https://api.sorare.com/federation/graphql"

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
    nextGame(so5FixtureEligible: true) {
      ... on Game {
        id
        date
        statusTyped
        homeTeam { name slug }
        awayTeam { name slug }
      }
    }
    allPlayerGameScores(first: 20) {
      nodes {
        score
        projectedScore
        scoreStatus
        positionTyped
        projection {
          grade
          reliabilityBasisPoints
          score
        }
        anyGame {
          date
          statusTyped
          homeTeam { name slug }
          awayTeam { name slug }
        }
        anyPlayerGameStats {
          minsPlayed
          gameStarted
          fieldStatus
          footballPlayingStatusOdds {
            reliability
            starterOddsBasisPoints
          }
        }
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


def avg_final_scores(nodes: list, n: int) -> Optional[float]:
    finals = [
        x["score"]
        for x in nodes
        if x.get("scoreStatus") == "FINAL" and isinstance(x.get("score"), (int, float))
    ]
    if not finals:
        return None
    take = finals[:n]
    return round(sum(take) / len(take), 1)


def find_upcoming(nodes: list) -> Optional[dict]:
    scheduled = []
    for node in nodes:
        game = node.get("anyGame") or {}
        st = str(game.get("statusTyped") or "").upper()
        if "SCHEDULE" in st or node.get("scoreStatus") == "PENDING":
            scheduled.append(node)
    if not scheduled:
        return None
    for node in scheduled:
        proj = node.get("projection")
        ps = node.get("projectedScore")
        if proj or (isinstance(ps, (int, float)) and ps > 0):
            return node
    return scheduled[0]


def pack_player(player: dict) -> dict:
    nodes = ((player.get("allPlayerGameScores") or {}).get("nodes")) or []
    position = normalize_position(player.get("anyPositions"))
    club = (player.get("activeClub") or {}).get("name")

    last5 = player.get("averageScore")
    last10 = player.get("averageScore10")
    last40 = player.get("averageScore40")
    if not isinstance(last5, (int, float)):
        last5 = avg_final_scores(nodes, 5)
    if not isinstance(last10, (int, float)):
        last10 = avg_final_scores(nodes, 10)
    if not isinstance(last40, (int, float)):
        last40 = avg_final_scores(nodes, 40)

    recent = []
    for x in nodes:
        if x.get("scoreStatus") != "FINAL":
            continue
        g = x.get("anyGame") or {}
        recent.append({
            "score": x.get("score"),
            "date": g.get("date"),
            "home": (g.get("homeTeam") or {}).get("name"),
            "away": (g.get("awayTeam") or {}).get("name"),
            "mins": (x.get("anyPlayerGameStats") or {}).get("minsPlayed"),
        })
        if len(recent) >= 5:
            break

    next_game_api = player.get("nextGame")
    upcoming_node = find_upcoming(nodes)
    has_fixture = bool(next_game_api) or bool(upcoming_node)

    fixture = None
    sorare_projection = None
    starter_odds_pct = None

    if upcoming_node:
        g = upcoming_node.get("anyGame") or {}
        stats = upcoming_node.get("anyPlayerGameStats") or {}
        odds = stats.get("footballPlayingStatusOdds") or {}
        if isinstance(odds.get("starterOddsBasisPoints"), int):
            starter_odds_pct = round(odds["starterOddsBasisPoints"] / 100, 1)

        fixture = {
            "kickoff": g.get("date"),
            "homeTeam": (g.get("homeTeam") or {}).get("name"),
            "awayTeam": (g.get("awayTeam") or {}).get("name"),
            "home": club and (g.get("homeTeam") or {}).get("name") == club,
            "status": g.get("statusTyped"),
        }

        proj = upcoming_node.get("projection")
        ps = upcoming_node.get("projectedScore")
        if proj or (isinstance(ps, (int, float)) and ps > 0):
            sorare_projection = {
                "score": proj.get("score") if proj and proj.get("score") is not None else ps,
                "grade": (proj or {}).get("grade"),
                "reliabilityPct": (
                    round(proj["reliabilityBasisPoints"] / 100, 1)
                    if proj and isinstance(proj.get("reliabilityBasisPoints"), int)
                    else None
                ),
            }
    elif next_game_api:
        fixture = {
            "kickoff": next_game_api.get("date"),
            "homeTeam": (next_game_api.get("homeTeam") or {}).get("name"),
            "awayTeam": (next_game_api.get("awayTeam") or {}).get("name"),
            "home": club and (next_game_api.get("homeTeam") or {}).get("name") == club,
            "status": next_game_api.get("statusTyped"),
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
            "recentScores": recent,
        },
        "hasUpcomingFixture": has_fixture,
        "fixture": fixture,
        "sorareProjection": sorare_projection if has_fixture else None,
        "starterOddsPct": starter_odds_pct if has_fixture else None,
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
    return {"status": "ok", "version": "0.3.1"}


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
        "version": "0.3.1",
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
