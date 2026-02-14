"""SM-liiga Rosters – minimal web app."""

import asyncio
from datetime import date, datetime, timezone, timedelta
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


def _today() -> date:
    """Return today's date in Finnish timezone (UTC+2/+3)."""
    finnish_tz = timezone(timedelta(hours=2))
    return datetime.now(finnish_tz).date()


def _is_future_game(g: dict, game_date: date, today: date, now_fi: datetime) -> bool:
    """Determine if a game hasn't started yet."""
    if game_date > today:
        return True
    if game_date < today:
        return False
    # Today – compare start time with current Finnish time
    start_str = g.get("start", "")
    if start_str:
        try:
            start_hour = int(start_str[11:13]) + 2  # UTC → Finnish
            start_minute = int(start_str[14:16])
            if start_hour > now_fi.hour or (
                start_hour == now_fi.hour and start_minute > now_fi.minute
            ):
                return True
        except (ValueError, IndexError):
            pass
    return False


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

    empty_ctx = {
        "request": request,
        "game_date": game_date,
        "game_date_fi": _fi_date(game_date),
        "prev_date": prev_date,
        "next_date": next_date,
        "roster_views": [],
        "future_views": [],
        "past_date_fi": None,
        "has_games": False,
    }

    if not games:
        return templates.TemplateResponse("index.html", empty_ctx)

    # Determine season from first game
    season = games[0].get("season", 2026)

    # Fetch season stats (cached)
    season_stats = await liiga_client.get_season_stats(season, tournament)

    # Build stats lookup by playerId
    stats_map: dict[int, dict] = {}
    if isinstance(season_stats, list):
        for s in season_stats:
            pid = s.get("playerId")
            if pid:
                stats_map[pid] = s

    # Split games into played and future
    today = _today()
    finnish_tz = timezone(timedelta(hours=2))
    now_fi = datetime.now(finnish_tz)

    played_games: list[dict] = []
    future_games: list[dict] = []
    for g in games:
        if _is_future_game(g, game_date, today, now_fi):
            future_games.append(g)
        else:
            played_games.append(g)

    # ── "One round from the past" fallback ──
    # If there are no played games on this date (e.g. today only has upcoming
    # games), also load the previous game date's results.
    past_date_fi: str | None = None
    if not played_games and prev_date:
        prev_date_obj = datetime.strptime(prev_date, "%Y-%m-%d").date()
        prev_games_data = await liiga_client.get_games_for_date(prev_date_obj, tournament)
        played_games = prev_games_data.get("games", [])
        prev_date = prev_games_data.get("previousGameDate") or prev_date
        past_date_fi = _fi_date(prev_date_obj)

        # Need season stats for prev date's season too (usually same)
        if played_games:
            prev_season = played_games[0].get("season", season)
            if prev_season != season:
                prev_stats = await liiga_client.get_season_stats(prev_season, tournament)
                if isinstance(prev_stats, list):
                    for s in prev_stats:
                        pid = s.get("playerId")
                        if pid:
                            stats_map[pid] = s

    # ── Fetch game details + match stats for played games ──
    roster_views: list[dict] = []
    if played_games:
        detail_tasks = [
            liiga_client.get_game_detail(season, g["id"])
            for g in played_games
        ]
        game_details = await asyncio.gather(*detail_tasks, return_exceptions=True)

        # Fetch per-player match stats for each game
        match_tasks = []
        valid_details = []
        for detail in game_details:
            if isinstance(detail, Exception):
                continue
            game_obj = detail.get("game", {})
            gid = game_obj.get("id")
            if gid is None:
                continue
            valid_details.append(detail)
            match_tasks.append(
                liiga_client.get_match_stats_for_game(detail, season, gid)
            )

        match_results = await asyncio.gather(*match_tasks, return_exceptions=True)

        for detail, match_res in zip(valid_details, match_results):
            ms_map = match_res if isinstance(match_res, dict) else {}
            try:
                view = liiga_client.build_roster_view(detail, stats_map, season, ms_map)
                roster_views.append(view)
            except Exception:
                continue

    # ── Build compact views for future games ──
    future_views: list[dict] = []
    for g in future_games:
        future_views.append({
            "homeTeam": g.get("homeTeam", {}),
            "awayTeam": g.get("awayTeam", {}),
            "start": g.get("start", ""),
            "iceRink": g.get("iceRink", ""),
        })

    return templates.TemplateResponse("index.html", {
        "request": request,
        "game_date": game_date,
        "game_date_fi": _fi_date(game_date),
        "prev_date": prev_date,
        "next_date": next_date,
        "roster_views": roster_views,
        "future_views": future_views,
        "past_date_fi": past_date_fi,
        "has_games": bool(roster_views or future_views),
    })
