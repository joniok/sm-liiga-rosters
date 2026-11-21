"""Client for the Liiga.fi API v2."""

import asyncio
from collections import Counter
import httpx
import random
import time
from datetime import date, datetime, timezone
from itertools import groupby
from zoneinfo import ZoneInfo
from typing import Any

LIIGA_API_BASE = "https://liiga.fi/api/v2"
TIMEOUT = 30.0

# Transient upstream failures (Liiga CDN / gateway); retry before failing the build
_GET_MAX_ATTEMPTS = 8
_GET_RETRY_BASE_DELAY_SEC = 1.25
_GET_RETRY_MAX_SLEEP_SEC = 45.0
_TRANSIENT_HTTP_STATUS = frozenset({408, 429, 502, 503, 504})

FI_TZ = ZoneInfo("Europe/Helsinki")


def game_start_helsinki(start_str: str | None) -> datetime | None:
    """Parse Liiga API game ``start`` (ISO-8601); return aware local time in Helsinki."""
    if not start_str:
        return None
    normalized = start_str.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(FI_TZ)


def format_game_start_time_fi(start_str: str | None) -> str | None:
    """Wall-clock start time in Finland (``HH:MM``), respecting DST."""
    dt = game_start_helsinki(start_str)
    return dt.strftime("%H:%M") if dt else None

TOURNAMENT_REGULAR = "runkosarja"
TOURNAMENT_PLAYOFFS = "playoffs"

# Next runkosarja game farther out than this → treat regular season as over for navigation
RUNKOSARJA_UPCOMING_HORIZON_DAYS = 200
# Playoff calendar considered “active” if prev/next game falls within this window of today
PLAYOFF_CALENDAR_WINDOW_DAYS = 45

# Simple TTL cache for season stats (they don't change often)
_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 300  # 5 minutes
CACHE_TTL_LONG = 3600  # 1 hour – for data that rarely changes

# Playoff series board: walk Liiga game-day chain (each step = one API call)
MAX_PLAYOFF_CHAIN_STEPS = 52
_po_games_cache: dict[str, tuple[float, list[dict]]] = {}
PO_GAMES_CACHE_TTL = 240  # seconds


async def _sleep_before_liiga_retry(attempt: int, resp: httpx.Response | None) -> None:
    """Backoff with jitter; honor Retry-After on 429 when present."""
    if resp is not None and resp.status_code == 429:
        ra = resp.headers.get("retry-after")
        if ra:
            try:
                await asyncio.sleep(min(float(ra), _GET_RETRY_MAX_SLEEP_SEC))
                return
            except ValueError:
                pass
    base = _GET_RETRY_BASE_DELAY_SEC * (2**attempt)
    jitter = random.uniform(0.0, 0.8)
    await asyncio.sleep(min(base + jitter, _GET_RETRY_MAX_SLEEP_SEC))


async def _get(path: str, params: dict | None = None) -> Any:
    """Make a GET request to the Liiga API."""
    url = f"{LIIGA_API_BASE}{path}"
    last_attempt = _GET_MAX_ATTEMPTS - 1
    for attempt in range(_GET_MAX_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.get(url, params=params)
        except httpx.RequestError:
            if attempt < last_attempt:
                await _sleep_before_liiga_retry(attempt, None)
                continue
            raise

        if resp.status_code in _TRANSIENT_HTTP_STATUS:
            if attempt < last_attempt:
                await _sleep_before_liiga_retry(attempt, resp)
                continue

        resp.raise_for_status()
        return resp.json()


async def _get_cached(cache_key: str, path: str, params: dict | None = None,
                      ttl: int | None = None) -> Any:
    """GET with in-memory TTL caching."""
    now = time.time()
    effective_ttl = ttl if ttl is not None else CACHE_TTL
    if cache_key in _cache:
        ts, data = _cache[cache_key]
        if now - ts < effective_ttl:
            return data
    data = await _get(path, params)
    _cache[cache_key] = (now, data)
    return data


def parse_api_date(dstr: str | None) -> date | None:
    """Parse a Liiga ``YYYY-MM-DD`` calendar date, or ``None`` if missing/invalid."""
    if not dstr:
        return None
    try:
        return datetime.strptime(str(dstr)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def usable_previous_game_date(dstr: str | None, today: date) -> date | None:
    """``previousGameDate`` only if it is strictly before *today*.

    During the offseason the API wraps this to the last game of the *upcoming*
    season (a future date). Treating that as the previous round is wrong.
    """
    d = parse_api_date(dstr)
    if d is None or d >= today:
        return None
    return d


def usable_next_game_date(dstr: str | None, today: date) -> date | None:
    """``nextGameDate`` only if it is today or later (not a wrapped past date)."""
    d = parse_api_date(dstr)
    if d is None or d < today:
        return None
    return d


async def get_games_for_date(game_date: date, tournament: str = TOURNAMENT_REGULAR) -> dict:
    """Fetch all games for a given date.

    Returns dict with keys: games, previousGameDate, nextGameDate.
    """
    return await _get("/games", params={
        "tournament": tournament,
        "date": game_date.isoformat(),
    })


async def get_game_detail(season: int, game_id: int) -> dict:
    """Fetch full game detail including rosters (homeTeamPlayers, awayTeamPlayers)."""
    return await _get(f"/games/{season}/{game_id}")


async def get_season_stats(season: int, tournament: str = TOURNAMENT_REGULAR) -> list[dict]:
    """Fetch all player season stats (skaters + goalies combined).

    Returns an empty list if the endpoint errors (e.g. new season not ready yet)
    so roster pages still render without season columns.
    """
    cache_key = f"stats-{season}-{tournament}"
    try:
        data = await _get_cached(cache_key, f"/players/stats/summed/{season}/{season}/{tournament}/true")
    except (httpx.HTTPStatusError, httpx.RequestError):
        return []
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# Per-player game log (for match-specific stats)
# ---------------------------------------------------------------------------

_log_inflight: dict[str, asyncio.Task] = {}
# True when that cache entry was fetched while a game on the page had not ended.
_log_fetched_live: dict[str, bool] = {}

# A log fetched while a game is on carries that game's partial numbers. Such a
# fetch is only reused for this long, so the final row replaces it quickly.
LIVE_LOG_TTL = 60


def _player_log_is_fresh(cache_key: str, now: float, live_read: bool) -> bool:
    """Whether a cached player log is safe for this read.

    The timestamp is the real fetch time. A live read, and a normal read of a
    copy taken during a live game, only trust the last ``LIVE_LOG_TTL`` seconds.
    A normal copy stays for the full hour, including for a live read that is
    still inside that short window.
    """
    ts, _data = _cache[cache_key]
    age = now - ts
    if live_read or _log_fetched_live.get(cache_key, False):
        return age < LIVE_LOG_TTL
    return age < CACHE_TTL_LONG


async def _fetch_player_log(cache_key: str, path: str, live: bool) -> dict:
    data = await _get(path)
    _cache[cache_key] = (time.time(), data)
    _log_fetched_live[cache_key] = live
    return data


async def get_player_game_log(player_id: int, season: int, live: bool = False) -> dict:
    """Fetch a player's per-game stats for the season (cached 1 h).

    ``live`` is set when a game on the page has not ended. Such a log is
    reused for only ``LIVE_LOG_TTL`` seconds, and an older cached copy is not
    reused for it, so a partial live row cannot outlive the game. A second
    live read inside that window reuses the same fetch. Parallel callers for
    the same player share one request.
    """
    cache_key = f"player-games-{player_id}-{season}"
    now = time.time()
    if cache_key in _cache and _player_log_is_fresh(cache_key, now, live):
        return _cache[cache_key][1]
    task = _log_inflight.get(cache_key)
    if task is None:
        task = asyncio.create_task(_fetch_player_log(
            cache_key, f"/players/info/{player_id}/games/{season}", live,
        ))
        _log_inflight[cache_key] = task
        try:
            return await task
        finally:
            _log_inflight.pop(cache_key, None)
    return await task


def subtract_match_from_season_column(game_detail: dict, stats_tournament: str) -> bool:
    """Whether season columns should be (season total − this game).

    A game that has started but not ended is left alone: the game log can
    already have a row while the season totals do not include it yet.
    """
    game = game_detail.get("game", {}) if isinstance(game_detail, dict) else {}
    if not game.get("ended"):
        return False
    is_po = (game.get("serie") or "").upper() == "PLAYOFFS"
    if stats_tournament == TOURNAMENT_PLAYOFFS:
        return is_po
    if stats_tournament == TOURNAMENT_REGULAR:
        return not is_po
    return True


def _game_log_key_for_detail(game_detail: dict) -> str:
    """Which segment in /players/info/.../games/{season} holds this game's stats."""
    game = game_detail.get("game", {}) if isinstance(game_detail, dict) else {}
    if (game.get("serie") or "").upper() == "PLAYOFFS":
        return "playoffs"
    return "regular"


def _log_key_for_tournament(stats_tournament: str) -> str:
    if stats_tournament == TOURNAMENT_PLAYOFFS:
        return "playoffs"
    return "regular"


def _skater_log_game_counted(entry: dict, now: datetime) -> bool:
    """A skater log row is a season game once its game has been played.

    The summed ``games`` counts every dressed game, including one where the
    skater got no ice time (checked against the feed: counting every past row
    matches it, ice-time-only does not). So a zero-ice row in a past game
    counts here too, which also matches the match-row subtraction. A row dated
    in the future is a scheduled game, and goalie rows are not skater games.
    A row with no usable date counts only with ice time or points.
    """
    if not isinstance(entry, dict) or entry.get("goalkeeper"):
        return False
    start = game_start_helsinki(entry.get("date"))
    if start is not None:
        return start <= now
    return bool(
        _as_int(entry.get("timeOnIce")) > 0
        or _as_int(entry.get("goals"))
        or _as_int(entry.get("assists"))
        or _as_int(entry.get("totalPoints"))
    )


def skater_counting_from_log(
    entries: list | None,
    skip_game_ids: set[int] | None = None,
    now: datetime | None = None,
) -> dict | None:
    """Sum played skater games from a player game log.

    Returns counting stats in the same shape as the summed season row
    (``points``, ``timeOnIceAvg``, ``shotsIntoGoal``). ``timeOnIceAvg`` is
    per counted game, as in the summed feed. ``None`` when the log has no
    counted skater games. ``skip_game_ids`` drops outings that have started
    but not ended: those stay on the match pill, and the season column is not
    reduced until the game is final.
    """
    if not entries:
        return None
    skip = skip_game_ids or set()
    now = now or datetime.now(FI_TZ)
    goals = assists = points = pim = plus_minus = toi = shots = 0
    counted = 0
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("gameId") in skip:
            continue
        if not _skater_log_game_counted(entry, now):
            continue
        counted += 1
        goals += _as_int(entry.get("goals"))
        assists += _as_int(entry.get("assists"))
        points += _as_int(entry.get("totalPoints"))
        pim += _as_int(entry.get("penaltyMinutes"))
        plus_minus += _as_int(entry.get("plusMinus"))
        toi += _as_int(entry.get("timeOnIce"))
        shots += _as_int(entry.get("shotsIntoGoal"))
    if counted == 0:
        return None
    return {
        "games": counted,
        "goals": goals,
        "assists": assists,
        "points": points,
        "penaltyMinutes": pim,
        "plusMinus": plus_minus,
        "timeOnIce": toi,
        "timeOnIceAvg": toi / counted,
        "shotsIntoGoal": shots,
    }


def _log_counting_is_fresher(log_totals: dict, summed: dict) -> bool:
    """True when the game log includes games the summed feed has not rolled up.

    A shorter log is ignored so a partial response cannot wipe a fuller total.
    """
    if log_totals["games"] < _as_int(summed.get("games")):
        return False
    if log_totals["games"] > _as_int(summed.get("games")):
        return True
    for key in ("goals", "assists", "points"):
        if log_totals[key] > _as_int(summed.get(key)):
            return True
    return False


async def merge_fresher_skater_logs(
    game_detail: dict,
    season: int,
    stats_map: dict,
    stats_tournament: str = TOURNAMENT_REGULAR,
    skip_game_ids: set[int] | None = None,
) -> None:
    """Fill stale summed skater totals from the game log.

    The summed stats feed can lag a finished game. The player log already
    has that outing, which is why a card can show 0+0 the day after 0+1.
    Goalies are left on the summed row (save percentage and GAA are not
    rebuilt from the log).

    Games in ``skip_game_ids``, and this game itself when it has not ended,
    are left out of the shared season totals. A live outing is already on
    the match pill, and ``subtract_match_from_season_column`` does not
    remove it until the game ends.
    """
    log_key = _log_key_for_tournament(stats_tournament)
    game = game_detail.get("game") or {}
    skip = set(skip_game_ids or ())
    game_id = game.get("id")
    if game_id is not None and not game.get("ended"):
        skip.add(game_id)
    player_ids: list[int] = []
    seen: set[int] = set()
    for key in ("homeTeamPlayers", "awayTeamPlayers"):
        for player in game_detail.get(key) or []:
            pid = player.get("id")
            if not pid or pid in seen:
                continue
            if _position_group(player.get("role"), player.get("roleCode")) == "G":
                continue
            seen.add(pid)
            player_ids.append(pid)
    if not player_ids:
        return

    sem = asyncio.Semaphore(25)
    # Any unfinished game on the page puts a partial row in these logs.
    live = bool(skip)
    now = datetime.now(FI_TZ)

    async def _one(pid: int) -> tuple[int, dict | None]:
        async with sem:
            try:
                data = await get_player_game_log(pid, season, live=live)
            except Exception:
                return pid, None
            entries = data.get(log_key, []) if isinstance(data, dict) else []
            return pid, skater_counting_from_log(entries, skip, now)

    results = await asyncio.gather(*[_one(pid) for pid in player_ids])
    for pid, totals in results:
        if not totals:
            continue
        current = stats_map.get(pid) or {}
        if not _log_counting_is_fresher(totals, current):
            continue
        merged = dict(current)
        merged.update(totals)
        # Keep the games count the summed xG covers. The log has no xG, so the
        # roster view compares shown games against this count to decide xGE.
        merged["feedGames"] = _as_int(current.get("feedGames", current.get("games")))
        stats_map[pid] = merged


async def is_runkosarja_schedule_active(game_day: date) -> bool:
    """True if runkosarja still has games today or a scheduled upcoming game this season."""
    data = await get_games_for_date(game_day, TOURNAMENT_REGULAR)
    if data.get("games"):
        return True
    nd = data.get("nextGameDate")
    if not nd:
        return False
    try:
        nd_d = datetime.strptime(nd, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    if nd_d < game_day:
        return False
    return (nd_d - game_day).days <= RUNKOSARJA_UPCOMING_HORIZON_DAYS


async def is_playoffs_calendar_near(game_day: date) -> bool:
    """True if the playoff bracket has (or recently/upcoming had) games around this date."""
    po = await get_games_for_date(game_day, TOURNAMENT_PLAYOFFS)
    if po.get("games"):
        return True
    for key in ("previousGameDate", "nextGameDate"):
        dstr = po.get(key)
        if not dstr:
            continue
        try:
            d = datetime.strptime(dstr, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if abs((d - game_day).days) <= PLAYOFF_CALENDAR_WINDOW_DAYS:
            return True
    return False


async def use_playoffs_for_games(game_day: date) -> bool:
    """Use playoff fixtures and playoff stats when regular season is over and playoffs are on."""
    if await is_runkosarja_schedule_active(game_day):
        return False
    return await is_playoffs_calendar_near(game_day)


async def _walk_playoff_one_direction(anchor: date, direction: str) -> set[date]:
    """Follow Liiga playoff previousGameDate or nextGameDate chain from anchor."""
    dates: set[date] = set()
    current = anchor
    seen_dir: set[date] = set()
    key = "previousGameDate" if direction == "prev" else "nextGameDate"
    for _ in range(MAX_PLAYOFF_CHAIN_STEPS):
        if current in seen_dir:
            break
        seen_dir.add(current)
        dates.add(current)
        data = await get_games_for_date(current, TOURNAMENT_PLAYOFFS)
        nxt = data.get(key)
        if not nxt:
            break
        try:
            current = datetime.strptime(nxt, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            break
    return dates


async def _walk_playoff_date_chain(anchor: date) -> set[date]:
    """All playoff calendar dates reachable via prev/next navigation from anchor (both directions)."""
    back, fwd = await asyncio.gather(
        _walk_playoff_one_direction(anchor, "prev"),
        _walk_playoff_one_direction(anchor, "next"),
    )
    return back | fwd


async def collect_playoff_games_for_board(anchor: date) -> list[dict]:
    """All playoff list games for the dominant season, discovered via date-chain walk."""
    cache_key = anchor.isoformat()
    now = time.time()
    if cache_key in _po_games_cache:
        ts, cached = _po_games_cache[cache_key]
        if now - ts < PO_GAMES_CACHE_TTL:
            return cached

    date_set = await _walk_playoff_date_chain(anchor)
    sem = asyncio.Semaphore(14)

    async def _fetch_day(d: date) -> dict:
        async with sem:
            return await get_games_for_date(d, TOURNAMENT_PLAYOFFS)

    payloads = await asyncio.gather(*[_fetch_day(d) for d in date_set])
    by_id: dict[int, dict] = {}
    for data in payloads:
        for g in data.get("games", []):
            if (g.get("serie") or "").upper() != "PLAYOFFS":
                continue
            gid = g.get("id")
            if gid is not None:
                by_id[gid] = g

    games = list(by_id.values())
    if not games:
        _po_games_cache[cache_key] = (now, games)
        return games

    season_counts = Counter(g.get("season") for g in games if g.get("season") is not None)
    if not season_counts:
        _po_games_cache[cache_key] = (now, games)
        return games
    main_season = season_counts.most_common(1)[0][0]
    filtered = [g for g in games if g.get("season") == main_season]
    _po_games_cache[cache_key] = (now, filtered)
    return filtered


def playoff_phase_label_fi(phase: int) -> str:
    """Liiga playOffPhase → section title (matches Liiga.fi naming)."""
    return {
        1: "1. Kierros",
        2: "Puolivälierät",
        3: "Välierät",
        4: "Pronssiottelu",
        5: "Finaalit",
    }.get(phase, f"Vaihe {phase}")


def _best_of_fi(req_wins: int) -> str:
    rw = int(req_wins) if req_wins else 3
    spelled = {3: "viidestä", 4: "seitsemästä", 5: "yhdeksästä"}
    if rw in spelled:
        return f"Paras {spelled[rw]}"
    return f"{rw} voittoon"


def _min_team_ranking(games: list[dict], team_id: str) -> int:
    r = 99
    for g in games:
        for side in ("homeTeam", "awayTeam"):
            t = g.get(side) or {}
            if t.get("teamId") != team_id:
                continue
            rank = t.get("ranking")
            if rank is not None:
                r = min(r, int(rank))
    return r


def _team_slice_from_games(games: list[dict], team_id: str) -> dict[str, Any]:
    for g in games:
        for side in ("homeTeam", "awayTeam"):
            t = g.get(side) or {}
            if t.get("teamId") == team_id:
                logos = t.get("logos") or {}
                return {
                    "teamId": team_id,
                    "name": t.get("teamName") or "",
                    "logo": logos.get("darkBg") or logos.get("lightBg"),
                }
    return {"teamId": team_id, "name": "", "logo": None}


def build_playoff_series_rows(games: list[dict]) -> list[dict[str, Any]]:
    """One row per (playOffPhase, playOffPair) with win counts and formatting for the template."""
    by_key: dict[tuple[int, int], list[dict]] = {}
    for g in games:
        ph = g.get("playOffPhase")
        pr = g.get("playOffPair")
        if ph is None or pr is None:
            continue
        by_key.setdefault((int(ph), int(pr)), []).append(g)

    rows: list[dict[str, Any]] = []
    for (phase, pair), gs in by_key.items():
        gs_sorted = sorted(
            gs,
            key=lambda x: x.get("start") or "",
        )
        team_ids: set[str] = set()
        for g in gs_sorted:
            ht = (g.get("homeTeam") or {}).get("teamId")
            at = (g.get("awayTeam") or {}).get("teamId")
            if ht:
                team_ids.add(ht)
            if at:
                team_ids.add(at)
        if len(team_ids) != 2:
            continue

        ids = list(team_ids)
        ranked = sorted(
            ids,
            key=lambda tid: (_min_team_ranking(gs_sorted, tid), tid),
        )
        tid_a, tid_b = ranked[0], ranked[1]

        wins: dict[str, int] = {tid_a: 0, tid_b: 0}
        for g in gs_sorted:
            if not g.get("ended"):
                continue
            h = g.get("homeTeam") or {}
            a = g.get("awayTeam") or {}
            hid, aid = h.get("teamId"), a.get("teamId")
            if not hid or not aid:
                continue
            hg = int(h.get("goals") or 0)
            ag = int(a.get("goals") or 0)
            if hg > ag and hid in wins:
                wins[hid] += 1
            elif ag > hg and aid in wins:
                wins[aid] += 1

        req = gs_sorted[0].get("playOffReqWins") or 3
        try:
            req_i = int(req)
        except (TypeError, ValueError):
            req_i = 3
        w1, w2 = wins.get(tid_a, 0), wins.get(tid_b, 0)
        decided = w1 >= req_i or w2 >= req_i

        t1 = _team_slice_from_games(gs_sorted, tid_a)
        t2 = _team_slice_from_games(gs_sorted, tid_b)
        rows.append({
            "phase": phase,
            "pair": pair,
            "team1": {"name": t1["name"], "logo": t1["logo"], "wins": w1},
            "team2": {"name": t2["name"], "logo": t2["logo"], "wins": w2},
            "best_of_label": _best_of_fi(req_i),
            "req_wins": req_i,
            "decided": decided,
        })

    rows.sort(key=lambda r: (-r["phase"], r["pair"]))
    return rows


async def get_playoff_series_board(anchor: date) -> list[dict[str, Any]]:
    """Grouped phase sections for the playoff summary strip (Liiga.fi–style)."""
    games = await collect_playoff_games_for_board(anchor)
    rows = build_playoff_series_rows(games)
    out: list[dict[str, Any]] = []
    for phase, grp in groupby(rows, key=lambda r: r["phase"]):
        out.append({
            "phase": phase,
            "phase_label": playoff_phase_label_fi(int(phase)),
            "series": list(grp),
        })
    return out


async def get_match_stats_for_game(
    game_detail: dict, season: int, game_id: int,
) -> dict[int, dict]:
    """Fetch per-game stats for every player in a game.

    Returns ``{player_id: row}`` for players who have a game-log entry for
    ``game_id``. Each row keeps ``goals``, ``assists``, ``points``,
    ``penaltyMinutes``, ``plusMinus`` and ``timeOnIce``. ``played`` is true
    for a skater with an entry, and for a goalie only when ``timeOnIce`` is
    above zero (a dressed backup gets a zero-minute row). Goalie rows also
    include ``gkWon``, ``gkTied``, ``gkLost`` and ``shutOut``. Callers that
    ignore the extra keys keep working.
    """
    # Collect all player IDs from the game detail
    player_ids: list[int] = []
    goalie_ids: set[int] = set()
    for key in ("homeTeamPlayers", "awayTeamPlayers"):
        for p in game_detail.get(key, []):
            pid = p.get("id")
            if pid:
                player_ids.append(pid)
                if _position_group(p.get("role"), p.get("roleCode")) == "G":
                    goalie_ids.add(pid)

    if not player_ids:
        return {}

    sem = asyncio.Semaphore(25)

    log_key = _game_log_key_for_detail(game_detail)
    # An unfinished game's row is partial; keep it out of the long cache.
    live = not (game_detail.get("game") or {}).get("ended")

    async def _fetch_one(pid: int) -> tuple[int, dict | None]:
        async with sem:
            try:
                data = await get_player_game_log(pid, season, live=live)
                entries = data.get(log_key, []) if isinstance(data, dict) else []
                if not entries and log_key == "playoffs":
                    entries = data.get("regular", []) if isinstance(data, dict) else []
                for entry in entries:
                    if entry.get("gameId") == game_id:
                        ice_time = _as_int(entry.get("timeOnIce"))
                        is_goalie = pid in goalie_ids or bool(entry.get("goalkeeper"))
                        row = {
                            "goals": entry.get("goals", 0) or 0,
                            "assists": entry.get("assists", 0) or 0,
                            "points": entry.get("totalPoints", 0) or 0,
                            "penaltyMinutes": entry.get("penaltyMinutes", 0) or 0,
                            "plusMinus": entry.get("plusMinus", 0) or 0,
                            "timeOnIce": ice_time,
                            # A dressed backup has a log row with zero ice time.
                            "played": ice_time > 0 if is_goalie else True,
                        }
                        if is_goalie:
                            row["gkWon"] = _as_int(entry.get("won"))
                            row["gkTied"] = _as_int(entry.get("tied"))
                            row["gkLost"] = _as_int(entry.get("lost"))
                            row["shutOut"] = _as_int(entry.get("shutOut"))
                        return pid, row
                return pid, None
            except Exception:
                return pid, None

    results = await asyncio.gather(*[_fetch_one(pid) for pid in player_ids])
    return {pid: stats for pid, stats in results if stats is not None}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_u20(date_of_birth: str | None, season: int) -> bool:
    """Check if a player qualifies as U20 for a given season.

    U20 means the player turns 20 or younger during the calendar year
    the season starts (season year - 1). For season 2026 (= 2025-26),
    the cutoff birth year is 2006 (born 2006 or later).
    """
    if not date_of_birth:
        return False
    try:
        dob = datetime.strptime(date_of_birth[:10], "%Y-%m-%d").date()
        season_start_year = season - 1
        cutoff_year = season_start_year - 19
        return dob.year >= cutoff_year
    except (ValueError, TypeError):
        return False


# Mapping of role codes from the roster API to display groups.
# 2026–27 stats system uses coarser roles: STRIKER / DEFENSEMAN / GOALIE
# until lines are published; extras use 13th forward / 7th+ defenseman.
FORWARD_ROLES = {
    "LEFT_WING", "RIGHT_WING", "CENTER", "STRIKER",
    "THIRTEENTH_STRIKER", "FOURTEENTH_STRIKER",
}
DEFENSE_ROLES = {
    "LEFT_DEFENSEMAN", "RIGHT_DEFENSEMAN", "DEFENSEMAN",
    "SEVENTH_DEFENSEMAN", "EIGHTH_DEFENSEMAN",
}
GOALIE_ROLES = {"GOALIE"}
FORWARD_CODES = {"VL", "OL", "KH", "H", "KP", "13. H", "14. H"}
DEFENSE_CODES = {"VP", "OP", "P", "7. P", "8. P"}
GOALIE_CODES = {"MV"}

# Dressed extras ("Lisäpelaajat"). Never a kenttä, even when the API sends line=4.
DRESSED_EXTRA_ROLES = {
    "THIRTEENTH_STRIKER", "FOURTEENTH_STRIKER",
    "SEVENTH_DEFENSEMAN", "EIGHTH_DEFENSEMAN",
}
DRESSED_EXTRA_CODES = {"13. H", "14. H", "7. P", "8. P"}

_FWD_SORT = {"LEFT_WING": 0, "CENTER": 1, "RIGHT_WING": 2}
_DEF_SORT = {"LEFT_DEFENSEMAN": 0, "RIGHT_DEFENSEMAN": 1}

# Short role labels for display (Finnish abbreviations)
ROLE_SHORT = {
    "LEFT_WING": "VLH",
    "RIGHT_WING": "OLH",
    "CENTER": "KH",
    "STRIKER": "H",
    "THIRTEENTH_STRIKER": "13.H",
    "FOURTEENTH_STRIKER": "14.H",
    "LEFT_DEFENSEMAN": "VP",
    "RIGHT_DEFENSEMAN": "OP",
    "DEFENSEMAN": "P",
    "SEVENTH_DEFENSEMAN": "7.P",
    "EIGHTH_DEFENSEMAN": "8.P",
    "GOALIE": "MV",
}

# Role codes from the roster / stats API
ROLE_CODE_SHORT = {
    "VL": "VLH",
    "OL": "OLH",
    "KH": "KH",
    "H": "H",
    "KP": "KP",
    "VP": "VP",
    "OP": "OP",
    "P": "P",
    "MV": "MV",
    "13. H": "13.H",
    "14. H": "14.H",
    "7. P": "7.P",
    "8. P": "8.P",
}

_BOOL_MERGE_KEYS = (
    "captain", "alternateCaptain", "rookie", "injured", "suspended", "removed",
)


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _at_least_zero(total: Any, subtracted: Any = 0) -> int:
    """Season count minus this game, never below zero."""
    return max(0, _as_int(total) - _as_int(subtracted))


def _goalie_actually_played(ms: dict) -> bool:
    """A goalie played only with ice time. A zero-minute log row is a backup.

    Maps built before ``timeOnIce`` was stored still use ``played``.
    """
    if not ms:
        return False
    if "timeOnIce" in ms:
        return _as_int(ms.get("timeOnIce")) > 0
    return bool(ms.get("played", True))


def _published_line(line: Any) -> int | None:
    """Positive line number, or None when the player is not on a kenttä."""
    if isinstance(line, bool) or line is None:
        return None
    try:
        n = int(line)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _is_dressed_extra(role: str | None, role_code: str | None) -> bool:
    return (role or "") in DRESSED_EXTRA_ROLES or (role_code or "") in DRESSED_EXTRA_CODES


def _jersey_key(player: dict) -> int:
    jersey = player.get("jersey")
    return jersey if isinstance(jersey, int) else 999


def _sort_forwards(forwards: list[dict]) -> None:
    forwards.sort(key=lambda p: (_FWD_SORT.get(p.get("role") or "", 9), _jersey_key(p)))


def _sort_defense(defensemen: list[dict]) -> None:
    defensemen.sort(key=lambda p: (_DEF_SORT.get(p.get("role") or "", 9), _jersey_key(p)))


def _sort_jersey(players: list[dict]) -> None:
    players.sort(key=_jersey_key)


def _role_short(role: str | None, role_code: str | None) -> str:
    if role and role in ROLE_SHORT:
        return ROLE_SHORT[role]
    if role_code and role_code in ROLE_CODE_SHORT:
        return ROLE_CODE_SHORT[role_code]
    if role_code:
        return str(role_code).replace(" ", "")
    return role or ""


def _position_group(role: str | None, role_code: str | None) -> str:
    """Return ``G``, ``F``, ``D``, or ``?``."""
    role = role or ""
    code = role_code or ""
    if role in GOALIE_ROLES or "GOALIE" in role or code in GOALIE_CODES:
        return "G"
    if role in FORWARD_ROLES or "STRIKER" in role or "WING" in role or role == "CENTER" or code in FORWARD_CODES:
        return "F"
    if role in DEFENSE_ROLES or "DEFENSE" in role or code in DEFENSE_CODES:
        return "D"
    return "?"


def _dedupe_roster_players(players: list[dict]) -> list[dict]:
    """The 2026–27 API often returns the same player twice (e.g. rookie flag)."""
    by_id: dict[int, dict] = {}
    order: list[int] = []
    passthrough: list[dict] = []
    for p in players:
        if p.get("removed"):
            continue
        pid = p.get("id")
        if pid is None:
            passthrough.append(p)
            continue
        if pid not in by_id:
            by_id[pid] = dict(p)
            order.append(pid)
            continue
        existing = by_id[pid]
        for key in _BOOL_MERGE_KEYS:
            existing[key] = bool(existing.get(key)) or bool(p.get(key))
        for key in ("line", "role", "roleCode", "jersey", "pictureUrl"):
            if not existing.get(key) and p.get(key):
                existing[key] = p.get(key)
    return [by_id[pid] for pid in order] + passthrough


def _add_helmet_and_u20_from_awards(
    award_list: list | None,
    golden_helmet_ids: set[int],
    redbull_ids: set[int],
    default_player_id: int | None = None,
) -> None:
    """Merge Kultakypärä / Red Bull U20 player ids from a list of award dicts."""
    for aw in award_list or []:
        name = (aw.get("awardName") or "").lower()
        pid = aw.get("playerId")
        if pid is None:
            pid = default_player_id
        if pid is None:
            continue
        if "kultainen kypärä" in name:
            golden_helmet_ids.add(pid)
        if "red bull" in name:
            redbull_ids.add(pid)


def build_roster_view(
    game_detail: dict,
    season_stats_map: dict,
    season: int,
    match_stats_map: dict[int, dict] | None = None,
    stats_tournament: str = TOURNAMENT_REGULAR,
) -> dict:
    """Build a structured roster view for a single game.

    ``match_stats_map`` is an optional mapping of
    ``{player_id: {goals, assists, points, penaltyMinutes, plusMinus, played}}``.
    Goalie rows may also include ``gkWon``, ``gkTied`` and ``gkLost``.
    When provided, season totals are adjusted (season minus this game) when
    ``stats_tournament`` matches the game's phase (e.g. runkosarja stats
    on a playoff game are not reduced by that playoff outing). ``games``
    drops by one when the player has a log entry; for goalies ``games`` is
    adjusted ``playedGames`` and the win/tie/loss columns follow the log.
    """
    if match_stats_map is None:
        match_stats_map = {}

    do_subtract = subtract_match_from_season_column(game_detail, stats_tournament)

    result: dict[str, Any] = {"game": {}}

    game = game_detail.get("game", {})
    awards = game_detail.get("awards", [])

    # Build award lookup: playerId -> list of award names
    award_map: dict[int, list[str]] = {}
    for aw in awards:
        pid = aw.get("playerId")
        if pid:
            award_map.setdefault(pid, []).append(aw.get("awardName", ""))

    golden_helmet_ids: set[int] = set()
    redbull_ids: set[int] = set()
    _add_helmet_and_u20_from_awards(awards, golden_helmet_ids, redbull_ids)

    # Liiga also attaches kultakypärä / Red Bull markers on each roster player's awards.
    for plist_key in ("homeTeamPlayers", "awayTeamPlayers"):
        for p in game_detail.get(plist_key) or []:
            pid = p.get("id")
            _add_helmet_and_u20_from_awards(
                p.get("awards"),
                golden_helmet_ids,
                redbull_ids,
                default_player_id=pid,
            )

    result["game"] = {
        "id": game.get("id"),
        "season": game.get("season"),
        "start": game.get("start"),
        "startTimeFi": format_game_start_time_fi(game.get("start")),
        "started": game.get("started"),
        "ended": game.get("ended"),
        "spectators": game.get("spectators"),
        "iceRink": game.get("iceRink"),
        "finishedType": game.get("finishedType"),
        "homeScore": game.get("homeTeam", {}).get("goals"),
        "awayScore": game.get("awayTeam", {}).get("goals"),
    }

    for side, players_key in [("homeTeam", "homeTeamPlayers"), ("awayTeam", "awayTeamPlayers")]:
        team_info = game.get(side, {})
        players = _dedupe_roster_players(game_detail.get(players_key) or [])
        any_line = any(_published_line(p.get("line")) is not None for p in players)

        team_data: dict[str, Any] = {
            "teamName": team_info.get("teamName", ""),
            "teamId": team_info.get("teamId", ""),
            "logos": team_info.get("logos", {}),
            "goals": team_info.get("goals", 0),
            "lines": [],
            "linesPublished": any_line,
            "dressedExtras": {"forwards": [], "defensemen": []},
            "scratches": {"forwards": [], "defensemen": [], "goalies": []},
            "goalies": [],
        }

        # Group players by line. An unpublished roster is the full squad,
        # later split into Maalivahdit / Puolustajat / Hyökkääjät.
        lines_map: dict[int, list[dict]] = {}
        goalies: list[dict] = []
        dressed_f: list[dict] = []
        dressed_d: list[dict] = []
        scratch_f: list[dict] = []
        scratch_d: list[dict] = []
        scratch_g: list[dict] = []
        unlined_forwards: list[dict] = []
        unlined_defense: list[dict] = []

        for p in players:
            line = p.get("line")
            role = p.get("role") or ""
            role_code = p.get("roleCode") or ""
            player_id = p.get("id")
            pos = _position_group(role, role_code)

            # Look up season stats
            stats = season_stats_map.get(player_id, {})

            # Look up match-specific stats
            ms = match_stats_map.get(player_id, {})
            m_goals = _as_int(ms.get("goals"))
            m_assists = _as_int(ms.get("assists"))
            m_points = _as_int(ms.get("points"))
            m_pim = _as_int(ms.get("penaltyMinutes"))
            m_pm = _as_int(ms.get("plusMinus"))

            sub_g = m_goals if do_subtract else 0
            sub_a = m_assists if do_subtract else 0
            sub_pt = m_points if do_subtract else 0
            sub_pim = m_pim if do_subtract else 0
            sub_pm = m_pm if do_subtract else 0

            # A row in match_stats_map is a game-log entry. ``played`` defaults
            # to true so older maps that only carry scoring keys still subtract.
            # Goalies need ice time: a dressed backup's row is all zeros.
            if pos == "G":
                played_this_game = _goalie_actually_played(ms)
            else:
                played_this_game = bool(ms) and bool(ms.get("played", True))
            game_delta = 1 if do_subtract and played_this_game else 0
            if pos == "G":
                # UI shows games actually played, not games dressed.
                season_games = _at_least_zero(stats.get("playedGames"), game_delta)
                gk_wins = _at_least_zero(stats.get("gkWins"), ms.get("gkWon") if game_delta else 0)
                gk_losses = _at_least_zero(stats.get("gkLosses"), ms.get("gkLost") if game_delta else 0)
                gk_ties = _at_least_zero(stats.get("gkTies"), ms.get("gkTied") if game_delta else 0)
                shutouts = _at_least_zero(stats.get("shutOut"), ms.get("shutOut") if game_delta else 0)
            else:
                season_games = _at_least_zero(stats.get("games"), game_delta)
                gk_wins = _as_int(stats.get("gkWins"))
                gk_losses = _as_int(stats.get("gkLosses"))
                gk_ties = _as_int(stats.get("gkTies"))
                shutouts = _as_int(stats.get("shutOut"))

            shown_goals = _at_least_zero(stats.get("goals"), sub_g)
            raw_u20 = stats.get("isU20") if isinstance(stats, dict) else None
            if raw_u20 is None:
                u20_flag = is_u20(p.get("dateOfBirth"), season)
            else:
                u20_flag = bool(raw_u20)
            expected_goals = _expected_goals(stats)
            # Shown games (after this game is removed) above the games the
            # summed xG covers: the goals come from a longer log, so xGE is not exact.
            xg_partial = pos != "G" and season_games > _as_int(
                stats.get("feedGames", stats.get("games")),
            )
            shots_on_goal = _as_int(stats.get("shotsIntoGoal"))

            player_data = {
                "id": player_id,
                "firstName": _title(p.get("firstName", "")),
                "lastName": _title(p.get("lastName", "")),
                "jersey": p.get("jersey"),
                "line": line,
                "role": role,
                "roleCode": role_code,
                "roleShort": _role_short(role, role_code),
                "pos": pos,
                "captain": p.get("captain", False),
                "alternateCaptain": p.get("alternateCaptain", False),
                "rookie": p.get("rookie", False),
                "injured": p.get("injured", False),
                "suspended": p.get("suspended", False),
                "pictureUrl": p.get("pictureUrl"),
                "dateOfBirth": p.get("dateOfBirth"),
                "isU20": u20_flag,
                "isBestU20": player_id in redbull_ids,
                "isGoldenHelmet": player_id in golden_helmet_ids,
                "nationality": p.get("nationality", ""),
                # Match-specific stats (this game only)
                "matchGoals": m_goals,
                "matchAssists": m_assists,
                "matchPoints": m_points,
                "matchPIM": m_pim,
                "matchPlusMinus": m_pm,
                "hasMatchStats": bool(m_goals or m_assists or m_pim or m_pm),
                "isStarter": False,
                # Adjusted season stats (season total minus this game)
                "games": season_games,
                "goals": shown_goals,
                "assists": _at_least_zero(stats.get("assists"), sub_a),
                "points": _at_least_zero(stats.get("points"), sub_pt),
                "plusMinus": _as_int(stats.get("plusMinus")) - sub_pm,
                "penaltyMinutes": _at_least_zero(stats.get("penaltyMinutes"), sub_pim),
                # xG stays the season total from the summed feed (this game is
                # still in it). xGE is shown goals minus that xG, unless the
                # goals come from a longer game log than the xG covers.
                "expectedGoals": expected_goals,
                "expectedGoalsPartial": xg_partial,
                "expectedGoalsDiff": None if xg_partial else shown_goals - expected_goals,
                "shotsOnGoalPct": (shown_goals / shots_on_goal * 100) if shots_on_goal else 0.0,
                "timeOnIceAvg": _format_toi(stats.get("timeOnIceAvg", 0)),
                # Averages stay season-level. Goalie W-T-L and shutouts are adjusted above.
                "savePercentage": stats.get("savePercentage", 0),
                "goalsAgainstAvg": stats.get("goalsAgainstAvg", 0),
                "gkWins": gk_wins,
                "gkLosses": gk_losses,
                "gkTies": gk_ties,
                "shutOut": shutouts,
            }

            line_num = _published_line(line)
            if not any_line:
                # Full squad, not a game roster. Drop skaters and goalies at 0 GP.
                # Announced lines keep debutants; that path does not use this filter.
                # Goalie ``games`` is games actually played.
                if (player_data.get("games") or 0) <= 0:
                    continue
                if pos == "D":
                    unlined_defense.append(player_data)
                elif pos == "G":
                    goalies.append(player_data)
                else:
                    unlined_forwards.append(player_data)
            elif _is_dressed_extra(role, role_code):
                if pos == "D":
                    dressed_d.append(player_data)
                else:
                    dressed_f.append(player_data)
            elif pos == "G":
                if line_num is None:
                    scratch_g.append(player_data)
                else:
                    goalies.append(player_data)
            elif line_num is None:
                # STRIKER/H and DEFENSEMAN/P with no line, plus any other unlined skater.
                if pos == "D":
                    scratch_d.append(player_data)
                else:
                    scratch_f.append(player_data)
            else:
                lines_map.setdefault(line_num, []).append(player_data)

        # Starter is the first goalie only after lines are published (line 1, then 2).
        goalies.sort(key=lambda g: (_published_line(g.get("line")) or 999, _jersey_key(g)))
        for i, g in enumerate(goalies):
            g["isStarter"] = bool(any_line and i == 0)

        for line_num in sorted(lines_map.keys()):
            line_players = lines_map[line_num]
            forwards = [p for p in line_players if p.get("pos") == "F"]
            defensemen = [p for p in line_players if p.get("pos") == "D"]
            unknown = [p for p in line_players if p.get("pos") not in {"F", "D", "G"}]
            forwards.extend(unknown)
            _sort_forwards(forwards)
            _sort_defense(defensemen)
            team_data["lines"].append({
                "lineNum": line_num,
                "label": f"{line_num}. kenttä",
                "forwards": forwards,
                "defensemen": defensemen,
            })

        if not any_line:
            _sort_jersey(unlined_forwards)
            _sort_jersey(unlined_defense)
            _sort_jersey(goalies)
            if unlined_defense:
                team_data["lines"].append({
                    "lineNum": None,
                    "label": "Puolustajat",
                    "forwards": [],
                    "defensemen": unlined_defense,
                })
            if unlined_forwards:
                team_data["lines"].append({
                    "lineNum": None,
                    "label": "Hyökkääjät",
                    "forwards": unlined_forwards,
                    "defensemen": [],
                })

        _sort_jersey(dressed_f)
        _sort_jersey(dressed_d)
        _sort_jersey(scratch_f)
        _sort_jersey(scratch_d)
        _sort_jersey(scratch_g)
        team_data["dressedExtras"] = {"forwards": dressed_f, "defensemen": dressed_d}
        team_data["scratches"] = {
            "forwards": scratch_f,
            "defensemen": scratch_d,
            "goalies": scratch_g,
        }
        team_data["goalies"] = goalies

        table_f: list[dict] = []
        table_d: list[dict] = []
        for line_data in team_data["lines"]:
            table_f.extend(line_data["forwards"])
            table_d.extend(line_data["defensemen"])
        table_f.extend(dressed_f)
        table_d.extend(dressed_d)
        _sort_jersey(table_f)
        _sort_jersey(table_d)
        team_data["tableForwards"] = table_f
        team_data["tableDefensemen"] = table_d

        # Best U20 skater per team via Red Bull award. If nobody has the award,
        # fall back to the highest-scoring player whose isU20 is true.
        if not redbull_ids:
            all_skaters: list[dict] = []
            for line_data in team_data["lines"]:
                all_skaters.extend(line_data["forwards"])
                all_skaters.extend(line_data["defensemen"])
            all_skaters.extend(dressed_f)
            all_skaters.extend(dressed_d)
            all_skaters.extend(scratch_f)
            all_skaters.extend(scratch_d)

            u20_skaters = [p for p in all_skaters if p.get("isU20")]
            if u20_skaters:
                u20_skaters.sort(key=lambda p: (-p["points"], -p["goals"]))
                top = u20_skaters[0]
                # Don't invent a Red Bull marker when nobody has scored yet
                if top["points"] or top["goals"]:
                    top["isBestU20"] = True

        result[side] = team_data

    result["hasLines"] = any(
        any(ln.get("lineNum") for ln in (result.get(side) or {}).get("lines") or [])
        for side in ("homeTeam", "awayTeam")
    )
    return result


def _title(name: str) -> str:
    """Convert 'MIRO' to 'Miro'."""
    if not name:
        return name
    return name.title()


def _expected_goals(stats: dict) -> float:
    """Season xG (maaliodottama). Not reduced by the current game."""
    try:
        return float(stats.get("expectedGoals") or 0)
    except (TypeError, ValueError):
        return 0.0


def _format_toi(seconds: float) -> str:
    """Format time-on-ice from seconds to MM:SS."""
    if not seconds:
        return "0:00"
    minutes = int(seconds) // 60
    secs = int(seconds) % 60
    return f"{minutes}:{secs:02d}"
