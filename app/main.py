"""SM-liiga Rosters – minimal web app."""

import asyncio
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import liiga_client

app = FastAPI(title="SM-liiga Rosters")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

FI_WEEKDAYS = ["Maanantai", "Tiistai", "Keskiviikko", "Torstai", "Perjantai", "Lauantai", "Sunnuntai"]


def _fi_date(d: date) -> str:
    """Format date in Finnish: 'Torstai 12.02.2026'."""
    return f"{FI_WEEKDAYS[d.weekday()]} {d.strftime('%d.%m.%Y')}"


# Mount static files
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, date: str | None = None, tournament: str = "runkosarja"):
    """Main page: show games for a date with rosters."""

    # Parse date or default to today
    if date:
        try:
            game_date = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            game_date = _today()
    else:
        game_date = _today()

    # Fetch games for the date
    games_data = await liiga_client.get_games_for_date(game_date, tournament)
    games = games_data.get("games", [])
    prev_date = games_data.get("previousGameDate")
    next_date = games_data.get("nextGameDate")

    if not games:
        return templates.TemplateResponse("index.html", {
            "request": request,
            "game_date": game_date,
            "game_date_fi": _fi_date(game_date),
            "prev_date": prev_date,
            "next_date": next_date,
            "roster_views": [],
            "has_games": False,
        })

    # Determine season from first game
    season = games[0].get("season", 2026)

    # Fetch season stats (cached per request – could add caching layer)
    season_stats = await liiga_client.get_season_stats(season, tournament)

    # Build stats lookup by playerId
    stats_map: dict[int, dict] = {}
    if isinstance(season_stats, list):
        for s in season_stats:
            pid = s.get("playerId")
            if pid:
                stats_map[pid] = s

    # Fetch game details in parallel
    detail_tasks = [
        liiga_client.get_game_detail(season, g["id"])
        for g in games
    ]
    game_details = await asyncio.gather(*detail_tasks, return_exceptions=True)

    # Build roster views
    roster_views = []
    for detail in game_details:
        if isinstance(detail, Exception):
            continue
        try:
            view = liiga_client.build_roster_view(detail, stats_map, season)
            roster_views.append(view)
        except Exception:
            continue

    return templates.TemplateResponse("index.html", {
        "request": request,
        "game_date": game_date,
        "game_date_fi": _fi_date(game_date),
        "prev_date": prev_date,
        "next_date": next_date,
        "roster_views": roster_views,
        "has_games": True,
    })


def _today() -> date:
    """Return today's date in Finnish timezone (UTC+2/+3)."""
    from datetime import timezone, timedelta
    finnish_tz = timezone(timedelta(hours=2))
    return datetime.now(finnish_tz).date()
