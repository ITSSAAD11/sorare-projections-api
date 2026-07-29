from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import List
import httpx
import asyncio

app = FastAPI(title="Sorare Projections API", version="0.1.0")

# Allow the Chrome extension to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SORARE_GRAPHQL = "https://api.sorare.com/graphql"


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
    """Temporary simple model. Will be replaced by real opponent-based model later."""
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
    else:  # MID
        xg = round(p * 0.016, 2)
        xa = round(p * 0.022, 2)

    return {
        "winPct": win_pct,
        "cleanSheetPct": clean_sheet_pct,
        "xg": xg,
        "xa": xa,
    }


async def fetch_player(slug: str):
    query = f"""
    query {{
      player(slug: "{slug}") {{
        slug
        displayName
        position
        so5Scores(last: 5) {{ score }}
        activeClub {{ name slug }}
      }}
    }}
    """
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.post(
            SORARE_GRAPHQL,
            json={"query": query},
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        data = r.json().get("data", {}).get("player")
        return data


@app.get("/")
def root():
    return {"status": "ok", "message": "Sorare Projections API is running"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/project")
async def project(slugs: str = Query(..., description="Comma-separated player slugs")):
    """
    Example: /project?slugs=andy-aryel-najar-rodriguez,nico-schlotterbeck
    """
    slug_list = [s.strip() for s in slugs.split(",") if s.strip()]
    slug_list = slug_list[:20]  # safety limit

    results = {}
    tasks = [fetch_player(slug) for slug in slug_list]
    players = await asyncio.gather(*tasks, return_exceptions=True)

    for slug, player in zip(slug_list, players):
        if isinstance(player, Exception) or player is None:
            results[slug] = None
            continue

        ps = compute_ps(player.get("so5Scores") or [])
        position = player.get("position") or "MID"
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
            # These will be filled later with real next-match data
            "opponent": None,
            "home": None,
            "kickoff": None,
        }

    return {"ok": True, "data": results}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
