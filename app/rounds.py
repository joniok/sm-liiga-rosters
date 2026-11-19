"""Shared previous/next round selection for the web app and static build."""

import asyncio
import logging
from datetime import date, datetime

from . import liiga_client

log = logging.getLogger(__name__)

FI_WEEKDAYS = [
    "Maanantai", "Tiistai", "Keskiviikko", "Torstai",
    "Perjantai", "Lauantai", "Sunnuntai",
]


def fi_date(d: date) -> str:
    """Format date in Finnish: 'Torstai 12.02.2026'."""
    return f"{FI_WEEKDAYS[d.weekday()]} {d.strftime('%d.%m.%Y')}"


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


def _stats_query_mode(raw: str | None) -> str:
    """Normalize ?stats= for playoff period (playoffs | runkosarja)."""
    if raw == liiga_client.TOURNAMENT_REGULAR:
        return liiga_client.TOURNAMENT_REGULAR
    return liiga_client.TOURNAMENT_PLAYOFFS


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

    match_tasks = []
    valid_details = []
    for detail in game_details:
        if isinstance(detail, Exception):
            log.error("game detail failed: %s", detail)
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
        gid = (detail.get("game") or {}).get("id")
        if isinstance(match_res, Exception):
            log.error("match stats failed for game %s: %s", gid, match_res)
            ms_map: dict = {}
        else:
            ms_map = match_res if isinstance(match_res, dict) else {}
        try:
            await liiga_client.merge_fresher_skater_logs(
                detail, season, stats_map, stats_tournament,
            )
            view = liiga_client.build_roster_view(
                detail, stats_map, season, ms_map, stats_tournament=stats_tournament,
            )
            roster_views.append(view)
        except Exception:
            log.exception("roster view failed for game %s", gid)
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
            log.error("game detail failed: %s", detail)
            continue
        gid = (detail.get("game") or {}).get("id")
        try:
            await liiga_client.merge_fresher_skater_logs(
                detail, season, stats_map, stats_tournament,
            )
            view = liiga_client.build_roster_view(
                detail, stats_map, season, stats_tournament=stats_tournament,
            )
            roster_views.append(view)
        except Exception:
            log.exception("roster view failed for game %s", gid)
    return roster_views


def _page_context(
    *,
    today: date,
    has_games_today: bool,
    prev_round_views: list[dict],
    prev_round_date: date | None,
    next_round_views: list[dict],
    next_round_date: date | None,
    show_stats_toggle: bool,
    stats_mode: str,
    playoff_series_phases: list,
) -> dict:
    return {
        "today_fi": fi_date(today),
        "has_games_today": has_games_today,
        "prev_round_views": prev_round_views,
        "prev_round_date_fi": fi_date(prev_round_date) if prev_round_date else None,
        "next_round_views": next_round_views,
        "next_round_date_fi": fi_date(next_round_date) if next_round_date else None,
        "next_round_is_today": next_round_date == today if next_round_date else False,
        "show_stats_toggle": show_stats_toggle,
        "stats_mode": stats_mode,
        "playoff_series_phases": playoff_series_phases,
    }


async def build_context(
    today: date,
    now_fi: datetime,
    stats_mode: str | None = None,
    *,
    static: bool = False,
) -> dict:
    """Load previous and next round views for ``today``.

    ``stats_mode`` is the raw ``?stats=`` value. During the playoffs it
    selects runkosarja only when it equals that tournament; otherwise
    playoff stats are used. Outside the playoffs the season is always
    runkosarja and the stats toggle stays off.

    ``static`` is the GitHub Pages build. That page cannot honor ``?stats=``,
    so the toggle stays off there even during the playoffs.
    """
    playoffs_branch = await liiga_client.use_playoffs_for_games(today)
    series_task: asyncio.Task | None = None
    if playoffs_branch:
        series_task = asyncio.create_task(liiga_client.get_playoff_series_board(today))

    games_tournament = (
        liiga_client.TOURNAMENT_PLAYOFFS if playoffs_branch else liiga_client.TOURNAMENT_REGULAR
    )
    show_stats_toggle = playoffs_branch and not static
    stats_tournament = (
        _stats_query_mode(stats_mode)
        if playoffs_branch
        else liiga_client.TOURNAMENT_REGULAR
    )

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

    all_games = prev_round_games + next_round_games
    if not all_games:
        playoff_series_phases = await series_task if series_task else []
        return _page_context(
            today=today,
            has_games_today=False,
            prev_round_views=[],
            prev_round_date=None,
            next_round_views=[],
            next_round_date=None,
            show_stats_toggle=show_stats_toggle,
            stats_mode=stats_tournament,
            playoff_series_phases=playoff_series_phases,
        )

    season = all_games[0].get("season", 2026)
    season_stats = await liiga_client.get_season_stats(season, stats_tournament)

    stats_map: dict[int, dict] = {}
    if isinstance(season_stats, list):
        for s in season_stats:
            pid = s.get("playerId")
            if pid:
                stats_map[pid] = s

    prev_round_views, next_round_views = await asyncio.gather(
        _build_played_round(prev_round_games, season, stats_map, stats_tournament),
        _build_upcoming_round(next_round_games, season, stats_map, stats_tournament),
    )
    playoff_series_phases = await series_task if series_task else []

    return _page_context(
        today=today,
        has_games_today=has_games_today,
        prev_round_views=prev_round_views,
        prev_round_date=prev_round_date,
        next_round_views=next_round_views,
        next_round_date=next_round_date,
        show_stats_toggle=show_stats_toggle,
        stats_mode=stats_tournament,
        playoff_series_phases=playoff_series_phases,
    )
