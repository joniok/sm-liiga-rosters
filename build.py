#!/usr/bin/env python3
"""Static site generator – renders SM-liiga Kokoonpanot to static HTML.

Usage:
    python build.py

Fetches live data from the Liiga.fi API and renders the Jinja2 template
to a self-contained static site in the ``output/`` directory.
"""

import asyncio
import shutil
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from app import liiga_client

OUTPUT_DIR = Path("output")
APP_DIR = Path("app")

FI_WEEKDAYS = [
    "Maanantai", "Tiistai", "Keskiviikko", "Torstai",
    "Perjantai", "Lauantai", "Sunnuntai",
]


# ---------------------------------------------------------------------------
# Helpers (mirrored from app/main.py to avoid FastAPI import side-effects)
# ---------------------------------------------------------------------------

def _fi_date(d: date) -> str:
    """Format date in Finnish: 'Torstai 12.02.2026'."""
    return f"{FI_WEEKDAYS[d.weekday()]} {d.strftime('%d.%m.%Y')}"


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
) -> tuple[list[dict], dict[str, dict[int, dict]]]:
    """Fetch details + match stats for played games and build roster views."""
    if not games:
        return [], {}

    detail_tasks = [liiga_client.get_game_detail(season, g["id"]) for g in games]
    game_details = await asyncio.gather(*detail_tasks, return_exceptions=True)

    match_tasks = []
    valid_details = []
    line_memory: dict[str, dict[int, dict]] = {}
    for detail in game_details:
        if isinstance(detail, Exception):
            continue
        game_obj = detail.get("game", {})
        gid = game_obj.get("id")
        if gid is None:
            continue
        valid_details.append(detail)
        liiga_client.merge_line_memory(line_memory, liiga_client.extract_line_memory(detail))
        match_tasks.append(liiga_client.get_match_stats_for_game(detail, season, gid))

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
    return roster_views, line_memory


async def _build_upcoming_round(
    games: list[dict],
    season: int,
    stats_map: dict[int, dict],
    stats_tournament: str,
    line_memory: dict[str, dict[int, dict]] | None = None,
) -> list[dict]:
    """Fetch details for upcoming games and build roster views."""
    if not games:
        return []

    detail_tasks = [liiga_client.get_game_detail(season, g["id"]) for g in games]
    game_details = await asyncio.gather(*detail_tasks, return_exceptions=True)

    roster_views: list[dict] = []
    for detail in game_details:
        if isinstance(detail, Exception):
            continue
        try:
            view = liiga_client.build_roster_view(
                detail, stats_map, season, stats_tournament=stats_tournament,
                line_memory=line_memory,
            )
            roster_views.append(view)
        except Exception:
            continue
    return roster_views


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------

async def build() -> None:
    """Fetch data from Liiga API and generate static HTML."""
    today = _today()
    now_fi = _now_fi()
    playoffs_branch = await liiga_client.use_playoffs_for_games(today)
    games_tournament = (
        liiga_client.TOURNAMENT_PLAYOFFS if playoffs_branch else liiga_client.TOURNAMENT_REGULAR
    )
    stats_tournament = (
        liiga_client.TOURNAMENT_PLAYOFFS if playoffs_branch else liiga_client.TOURNAMENT_REGULAR
    )

    print(f"Building for {_fi_date(today)} …")

    # ── Fetch today's games ──
    today_data = await liiga_client.get_games_for_date(today, games_tournament)
    today_games = today_data.get("games", [])
    api_prev_date = today_data.get("previousGameDate")
    api_next_date = today_data.get("nextGameDate")

    played_today: list[dict] = []
    upcoming_today: list[dict] = []
    for g in today_games:
        if _is_future_game(g, today, today, now_fi):
            upcoming_today.append(g)
        else:
            played_today.append(g)

    has_games_today = bool(today_games)

    # ── Previous round ──
    prev_round_games: list[dict] = []
    prev_round_date: date | None = None

    if played_today:
        prev_round_games = played_today
        prev_round_date = today
    elif api_prev_date:
        prev_date_obj = liiga_client.usable_previous_game_date(api_prev_date, today)
        if prev_date_obj:
            prev_data = await liiga_client.get_games_for_date(prev_date_obj, games_tournament)
            prev_round_games = prev_data.get("games", [])
            prev_round_date = prev_date_obj

    # ── Next round ──
    next_round_games: list[dict] = []
    next_round_date: date | None = None

    if upcoming_today:
        next_round_games = upcoming_today
        next_round_date = today
    elif api_next_date:
        next_date_obj = liiga_client.usable_next_game_date(api_next_date, today)
        if next_date_obj:
            next_data = await liiga_client.get_games_for_date(next_date_obj, games_tournament)
            next_round_games = next_data.get("games", [])
            next_round_date = next_date_obj

    # ── Season stats ──
    all_games = prev_round_games + next_round_games
    prev_round_views: list[dict] = []
    next_round_views: list[dict] = []

    if all_games:
        season = all_games[0].get("season", 2026)
        season_stats = await liiga_client.get_season_stats(season, stats_tournament)

        stats_map: dict[int, dict] = {}
        if isinstance(season_stats, list):
            for s in season_stats:
                pid = s.get("playerId")
                if pid:
                    stats_map[pid] = s

        prev_round_views, line_memory = await _build_played_round(
            prev_round_games, season, stats_map, stats_tournament,
        )
        next_round_views = await _build_upcoming_round(
            next_round_games, season, stats_map, stats_tournament, line_memory=line_memory,
        )

    playoff_series_phases: list = []
    if playoffs_branch:
        playoff_series_phases = await liiga_client.get_playoff_series_board(today)

    # ── Render template ──
    env = Environment(loader=FileSystemLoader(str(APP_DIR / "templates")))
    template = env.get_template("index.html")

    build_time = now_fi.strftime("%d.%m.%Y %H:%M")

    html = template.render(
        today_fi=_fi_date(today),
        has_games_today=has_games_today,
        prev_round_views=prev_round_views,
        prev_round_date_fi=_fi_date(prev_round_date) if prev_round_date else None,
        next_round_views=next_round_views,
        next_round_date_fi=_fi_date(next_round_date) if next_round_date else None,
        next_round_is_today=next_round_date == today if next_round_date else False,
        build_time=build_time,
        show_stats_toggle=False,
        stats_mode=stats_tournament,
        playoff_series_phases=playoff_series_phases,
    )

    # ── Write output ──
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True)

    (OUTPUT_DIR / "index.html").write_text(html, encoding="utf-8")

    # Copy static assets
    shutil.copytree(APP_DIR / "static", OUTPUT_DIR / "static")

    # .nojekyll prevents GitHub Pages from running Jekyll
    (OUTPUT_DIR / ".nojekyll").touch()

    print(f"Static site written to {OUTPUT_DIR}/ (built at {build_time})")


if __name__ == "__main__":
    asyncio.run(build())
