from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import List
import httpx
import asyncio

app = FastAPI(title="Sorare Projections API", version="0.1.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SORARE_GRAPHQL = "https://api.sorare.com/federation/graphql"


def clamp(v, min_v, max_v):
    return max(min_v, min(max_v, v))


def compute_ps(so5_scores):
    if not so5_scores:
        return None
    valid = [s["score"] for s in so5_scores if isinstance(s.get("score"), (int, float))]
    if not valid:
        return None
    return round(sum(valid) / len(valid), 1)


def derive_estimates(ps, position):
    pos = (position or "MID").upper()
    p = ps if isinstance(ps, (int, float)) else 40.0

    win_pct = clamp(round(48 + (p - 40) * 1.2), 5, 95)

    clean_sheet_pct = None
    xg = None
    xa = None

    if pos in ("GK", "DEF", "DF"):
        clean_sheet_pct = clamp(round(18 + (p - 30) * 1.05), 3, 80)
        xg = round(p * 0.008, 2)
        xa = round(p * 0.010, 2)
    elif pos in ("ATT", "FW"):
        xg = round(p * 0.030, 2)
        xa = round(p * 0.015, 2)
    else:
        xg = round(p * 0.016, 2)
        xa = round(p * 0.022, 2)

    return {
        "winPct": win_pct,
        "cleanSheetPct": clean_sheet_pct,
        "xg": xg,
        "xa": xa,
    }


async def fetch_player(slug: str):
    query = """
    query PlayersQuery($slugs: [String!]!) {
      players(slugs: $slugs) {
        slug
        displayName
        anyPositions
        so5Scores(last: 5) { score }
        activeClub { name slug }
      }
    }
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                SORARE_GRAPHQL,
                json={"query": query, "variables": {"slugs": [slug]}},
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (compatible; SorareProjections/1.0)",
                    "Accept": "application/json",
                },
            )
            body = r.json()

            if r.status_code != 200 or body.get("errors"):
                return {
                    "_debug": {
                        "status": r.status_code,
                        "errors": body.get("errors"),
                        "data": body.get("data"),
                        "slug_tried": slug,
                    }
                }

            players = (body.get("data") or {}).get("players") or []
            if not players:
                return {
                    "_debug": {
                        "status": r.status_code,
                        "errors": None,
                        "data": body.get("data"),
                        "slug_tried": slug,
                        "note": "players list empty",
                    }
                }
            return players[0]
    except Exception as e:
        return {"_debug": {"exception": str(e), "slug_tried": slug}}


@app.get("/")
def root():
    return {"status": "ok", "message": "Sorare Projections API is running", "version": "0.1.1"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/project")
async def project(slugs: str = Query(..., description="Comma-separated player slugs")):
    slug_list = [s.strip() for s in slugs.split(",") if s.strip()][:10]

    results = {}
    tasks = [fetch_player(slug) for slug in slug_list]
    players = await asyncio.gather(*tasks)

    for slug, player in zip(slug_list, players):
        if player is None or "_debug" in player:
            results[slug] = player  # keep the debug info
            continue

        ps = compute_ps(player.get("so5Scores") or [])
        # anyPositions is usually a list like ["Defender"] or ["Midfielder"]
        raw_pos = player.get("anyPositions") or []
        if isinstance(raw_pos, list) and raw_pos:
            position = str(raw_pos[0])
        else:
            position = "MID"
        estimates = derive_estimates(ps, position)

        results[slug] = {
            "slug": player.get("slug"),
            "displayName": player.get("displayName"),
            "position": position,
            "ps": ps,
            "winPct": estimates["winPct"],
            "cleanSheetPct": estimates["cleanSheetPct"],
            "xg": estimates["xg"],
            "xa": estimates["xa"],
            "club": (player.get("activeClub") or {}).get("name"),
            "source": "sorare+model",
            "opponent": None,
            "home": None,
            "kickoff": None,
        }

    return {"ok": True, "data": results}

@app.get("/debug")
async def debug(slug: str):
    query = """
    query($slugs: [String!]!) {
      players(slugs: $slugs) {
        __typename
        slug
        displayName
      }
    }
    """

    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            SORARE_GRAPHQL,
            json={
                "query": query,
                "variables": {"slugs": [slug]}
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0"
            }
        )

    try:
        body = r.json()
    except Exception:
        body = r.text

    return {
        "status": r.status_code,
        "body": body
    }
    return {
        "status": r.status_code,
        "body": r.json()
    }
