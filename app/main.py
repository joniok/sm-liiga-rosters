"""SM-liiga Rosters – minimal web app (round-based view)."""

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


def _today() -> date:
    """Return today's date in Europe/Helsinki (EET / EEST)."""
    return datetime.now(liiga_client.FI_TZ).date()


def _now_fi() -> datetime:
    """Return the current datetime in Europe/Helsinki."""
    return datetime.now(liiga_client.FI_TZ)


def _is_future_game(g: dict, game_date: date, today: date, now_fi: datetime) -> bool:
    """Determine if a game hasn't started yet."""
    if game_date > today:
        return True
    if game_date < today:
        return False
    start_fi = liiga_client.game_start_helsinki(g.get("start") or "")
    if start_fi is not None:
        return start_fi > now_fi
    return False


async def _build_played_round(
    games: list[dict],
    season: int,
    stats_map: dict[int, dict],
    stats_tournament: str,
) -> list[dict]:
    """Fetch details + match stats for played games and build roster views."""
    if not games:
        return []

    detail_tasks = [
        liiga_client.get_game_detail(season, g["id"])
        for g in games
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

    roster_views: list[dict] = []
    for detail, match_res in zip(valid_details, match_results):
        ms_map = match_res if isinstance(match_res, dict) else {}
        try:
            view = liiga_client.build_roster_view(
                detail, stats_map, season, ms_map, stats_tournament=stats_tournament,
            )
            roster_views.append(view)
        except Exception:
            continue
    return roster_views


async def _build_upcoming_round(
    games: list[dict],
    season: int,
    stats_map: dict[int, dict],
    stats_tournament: str,
) -> list[dict]:
    """Fetch details for upcoming games and build roster views (no match stat subtraction)."""
    if not games:
        return []

    detail_tasks = [
        liiga_client.get_game_detail(season, g["id"])
        for g in games
    ]
    game_details = await asyncio.gather(*detail_tasks, return_exceptions=True)

    roster_views: list[dict] = []
    for detail in game_details:
        if isinstance(detail, Exception):
            continue
        try:
            # No match_stats_map → season stats shown as-is
            view = liiga_client.build_roster_view(
                detail, stats_map, season, stats_tournament=stats_tournament,
            )
            roster_views.append(view)
        except Exception:
            continue
    return roster_views


def _stats_query_mode(raw: str | None) -> str:
    """Normalize ?stats= for playoff period (playoffs | runkosarja)."""
    if raw == liiga_client.TOURNAMENT_REGULAR:
        return liiga_client.TOURNAMENT_REGULAR
    return liiga_client.TOURNAMENT_PLAYOFFS


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, stats: str | None = None):
    """Main page: show previous round results and next round rosters."""

    today = _today()
    now_fi = _now_fi()

    playoffs_branch = await liiga_client.use_playoffs_for_games(today)
    series_task: asyncio.Task | None = None
    if playoffs_branch:
        series_task = asyncio.create_task(liiga_client.get_playoff_series_board(today))

    games_tournament = (
        liiga_client.TOURNAMENT_PLAYOFFS if playoffs_branch else liiga_client.TOURNAMENT_REGULAR
    )
    show_stats_toggle = playoffs_branch
    stats_tournament = (
        _stats_query_mode(stats)
        if playoffs_branch
        else liiga_client.TOURNAMENT_REGULAR
    )

    # ── Fetch today's games to orient ourselves ──
    today_data = await liiga_client.get_games_for_date(today, games_tournament)
    today_games = today_data.get("games", [])
    api_prev_date = today_data.get("previousGameDate")
    api_next_date = today_data.get("nextGameDate")

    # Split today's games into played and upcoming
    played_today: list[dict] = []
    upcoming_today: list[dict] = []
    for g in today_games:
        if _is_future_game(g, today, today, now_fi):
            upcoming_today.append(g)
        else:
            played_today.append(g)

    has_games_today = bool(today_games)

    # ── Determine previous round ──
    prev_round_games: list[dict] = []
    prev_round_date: date | None = None

    if played_today:
        # Today has played games → they are the previous round
        prev_round_games = played_today
        prev_round_date = today
    elif api_prev_date:
        # No played games today → load previous game date (ignore offseason wrap)
        prev_date_obj = liiga_client.usable_previous_game_date(api_prev_date, today)
        if prev_date_obj:
            prev_data = await liiga_client.get_games_for_date(prev_date_obj, games_tournament)
            prev_round_games = prev_data.get("games", [])
            prev_round_date = prev_date_obj

    # ── Determine next round ──
    next_round_games: list[dict] = []
    next_round_date: date | None = None

    if upcoming_today:
        # Today has upcoming games → they are the next round
        next_round_games = upcoming_today
        next_round_date = today
    elif api_next_date:
        # No upcoming games today → load next game date (ignore wrapped past dates)
        next_date_obj = liiga_client.usable_next_game_date(api_next_date, today)
        if next_date_obj:
            next_data = await liiga_client.get_games_for_date(next_date_obj, games_tournament)
            next_round_games = next_data.get("games", [])
            next_round_date = next_date_obj

    # ── Determine season and fetch season stats ──
    all_games = prev_round_games + next_round_games
    if not all_games:
        playoff_series_phases = await series_task if series_task else []
        return templates.TemplateResponse("index.html", {
            "request": request,
            "today_fi": _fi_date(today),
            "has_games_today": False,
            "prev_round_views": [],
            "prev_round_date_fi": None,
            "next_round_views": [],
            "next_round_date_fi": None,
            "next_round_is_today": False,
            "show_stats_toggle": show_stats_toggle,
            "stats_mode": stats_tournament,
            "playoff_series_phases": playoff_series_phases,
        })

    season = all_games[0].get("season", 2026)
    season_stats = await liiga_client.get_season_stats(season, stats_tournament)

    stats_map: dict[int, dict] = {}
    if isinstance(season_stats, list):
        for s in season_stats:
            pid = s.get("playerId")
            if pid:
                stats_map[pid] = s

    # ── Build roster views for both rounds (in parallel) ──
    prev_task = _build_played_round(prev_round_games, season, stats_map, stats_tournament)
    next_task = _build_upcoming_round(next_round_games, season, stats_map, stats_tournament)

    prev_round_views, next_round_views = await asyncio.gather(prev_task, next_task)
    playoff_series_phases = await series_task if series_task else []

    return templates.TemplateResponse("index.html", {
        "request": request,
        "today_fi": _fi_date(today),
        "has_games_today": has_games_today,
        "prev_round_views": prev_round_views,
        "prev_round_date_fi": _fi_date(prev_round_date) if prev_round_date else None,
        "next_round_views": next_round_views,
        "next_round_date_fi": _fi_date(next_round_date) if next_round_date else None,
        "next_round_is_today": next_round_date == today if next_round_date else False,
        "show_stats_toggle": show_stats_toggle,
        "stats_mode": stats_tournament,
        "playoff_series_phases": playoff_series_phases,
    })
