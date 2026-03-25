"""Client for the Liiga.fi API v2."""

import asyncio
from collections import Counter
import httpx
import time
from datetime import date, datetime
from itertools import groupby
from typing import Any

LIIGA_API_BASE = "https://liiga.fi/api/v2"
TIMEOUT = 30.0

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


async def _get(path: str, params: dict | None = None) -> Any:
    """Make a GET request to the Liiga API."""
    url = f"{LIIGA_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(url, params=params)
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
    """Fetch all player season stats (skaters + goalies combined)."""
    cache_key = f"stats-{season}-{tournament}"
    return await _get_cached(cache_key, f"/players/stats/summed/{season}/{season}/{tournament}/true")


# ---------------------------------------------------------------------------
# Per-player game log (for match-specific stats)
# ---------------------------------------------------------------------------

async def get_player_game_log(player_id: int, season: int) -> dict:
    """Fetch a player's per-game stats for the season (cached 1 h)."""
    cache_key = f"player-games-{player_id}-{season}"
    return await _get_cached(
        cache_key, f"/players/info/{player_id}/games/{season}", ttl=CACHE_TTL_LONG,
    )


def subtract_match_from_season_column(game_detail: dict, stats_tournament: str) -> bool:
    """Whether season columns should be (season total − this game)."""
    is_po = (game_detail.get("game", {}).get("serie") or "").upper() == "PLAYOFFS"
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

    rows.sort(key=lambda r: (r["phase"], r["pair"]))
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

    Returns {player_id: {goals, assists, points, penaltyMinutes, plusMinus}}
    for those players who have data for the given game_id.
    """
    # Collect all player IDs from the game detail
    player_ids: list[int] = []
    for key in ("homeTeamPlayers", "awayTeamPlayers"):
        for p in game_detail.get(key, []):
            pid = p.get("id")
            if pid:
                player_ids.append(pid)

    if not player_ids:
        return {}

    sem = asyncio.Semaphore(25)

    log_key = _game_log_key_for_detail(game_detail)

    async def _fetch_one(pid: int) -> tuple[int, dict | None]:
        async with sem:
            try:
                data = await get_player_game_log(pid, season)
                entries = data.get(log_key, []) if isinstance(data, dict) else []
                if not entries and log_key == "playoffs":
                    entries = data.get("regular", []) if isinstance(data, dict) else []
                for entry in entries:
                    if entry.get("gameId") == game_id:
                        return pid, {
                            "goals": entry.get("goals", 0) or 0,
                            "assists": entry.get("assists", 0) or 0,
                            "points": entry.get("totalPoints", 0) or 0,
                            "penaltyMinutes": entry.get("penaltyMinutes", 0) or 0,
                            "plusMinus": entry.get("plusMinus", 0) or 0,
                        }
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


# Mapping of role codes from the roster API to display groups
FORWARD_ROLES = {"LEFT_WING", "RIGHT_WING", "CENTER"}
DEFENSE_ROLES = {"LEFT_DEFENSEMAN", "RIGHT_DEFENSEMAN", "DEFENSEMAN"}
GOALIE_ROLES = {"GOALIE"}

# Short role labels for display (Finnish abbreviations)
ROLE_SHORT = {
    "LEFT_WING": "VLH",
    "RIGHT_WING": "OLH",
    "CENTER": "KH",
    "LEFT_DEFENSEMAN": "VP",
    "RIGHT_DEFENSEMAN": "OP",
    "DEFENSEMAN": "P",
    "GOALIE": "MV",
}

# Role codes from stats API
ROLE_CODE_SHORT = {
    "VL": "LW",
    "OL": "RW",
    "KH": "C",
    "H": "F",
    "VP": "LD",
    "OP": "RD",
    "P": "D",
    "MV": "G",
}


def build_roster_view(
    game_detail: dict,
    season_stats_map: dict,
    season: int,
    match_stats_map: dict[int, dict] | None = None,
    stats_tournament: str = TOURNAMENT_REGULAR,
) -> dict:
    """Build a structured roster view for a single game.

    ``match_stats_map`` is an optional mapping of
    ``{player_id: {goals, assists, points, penaltyMinutes, plusMinus}}``
    containing the per-game stats for this match.  When provided the
    season totals shown will be *adjusted* (season minus match) when
    ``stats_tournament`` matches the game's phase (e.g. runkosarja stats
    on a playoff game are not reduced by that playoff outing).
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

    # Find golden helmet players from awards
    golden_helmet_ids: set[int] = set()
    for aw in awards:
        if "kultainen kypärä" in (aw.get("awardName", "")).lower():
            golden_helmet_ids.add(aw.get("playerId"))

    # Find Red Bull U20 player from awards
    redbull_ids: set[int] = set()
    for aw in awards:
        if "red bull" in (aw.get("awardName", "")).lower():
            redbull_ids.add(aw.get("playerId"))

    result["game"] = {
        "id": game.get("id"),
        "season": game.get("season"),
        "start": game.get("start"),
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
        players = game_detail.get(players_key, [])

        team_data: dict[str, Any] = {
            "teamName": team_info.get("teamName", ""),
            "teamId": team_info.get("teamId", ""),
            "logos": team_info.get("logos", {}),
            "goals": team_info.get("goals", 0),
            "lines": [],
            "goalies": [],
            "extras": [],
        }

        # Group players by line
        lines_map: dict[int, list[dict]] = {}
        goalies: list[dict] = []
        extras: list[dict] = []

        for p in players:
            line = p.get("line")
            role = p.get("role", "")
            player_id = p.get("id")

            # Look up season stats
            stats = season_stats_map.get(player_id, {})

            # Look up match-specific stats
            ms = match_stats_map.get(player_id, {})
            m_goals = ms.get("goals", 0)
            m_assists = ms.get("assists", 0)
            m_points = ms.get("points", 0)
            m_pim = ms.get("penaltyMinutes", 0)
            m_pm = ms.get("plusMinus", 0)

            sub_g = m_goals if do_subtract else 0
            sub_a = m_assists if do_subtract else 0
            sub_pt = m_points if do_subtract else 0
            sub_pim = m_pim if do_subtract else 0
            sub_pm = m_pm if do_subtract else 0

            # Raw season totals
            s_goals = stats.get("goals", 0)
            s_assists = stats.get("assists", 0)
            s_points = stats.get("points", 0)
            s_pim = stats.get("penaltyMinutes", 0)
            s_pm = stats.get("plusMinus", 0)

            player_data = {
                "id": player_id,
                "firstName": _title(p.get("firstName", "")),
                "lastName": _title(p.get("lastName", "")),
                "jersey": p.get("jersey"),
                "line": line,
                "role": role,
                "roleShort": ROLE_SHORT.get(role, role),
                "captain": p.get("captain", False),
                "alternateCaptain": p.get("alternateCaptain", False),
                "rookie": p.get("rookie", False),
                "injured": p.get("injured", False),
                "suspended": p.get("suspended", False),
                "pictureUrl": p.get("pictureUrl"),
                "dateOfBirth": p.get("dateOfBirth"),
                "isU20": is_u20(p.get("dateOfBirth"), season),
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
                # Adjusted season stats (season total minus this game)
                "games": stats.get("games", 0),
                "goals": s_goals - sub_g,
                "assists": s_assists - sub_a,
                "points": s_points - sub_pt,
                "plusMinus": s_pm - sub_pm,
                "penaltyMinutes": s_pim - sub_pim,
                "powerplayGoals": stats.get("powerplayGoals", 0),
                "shots": stats.get("shots", 0),
                "shotPercentage": stats.get("shotPercentage", 0),
                "timeOnIceAvg": _format_toi(stats.get("timeOnIceAvg", 0)),
                # Goalie stats (not adjusted – averages don't subtract)
                "savePercentage": stats.get("savePercentage", 0),
                "goalsAgainstAvg": stats.get("goalsAgainstAvg", 0),
                "gkWins": stats.get("gkWins", 0),
                "gkLosses": stats.get("gkLosses", 0),
                "gkTies": stats.get("gkTies", 0),
                "shutOut": stats.get("shutOut", 0),
            }

            if role in GOALIE_ROLES:
                if line and line > 0:
                    goalies.append(player_data)
            elif line and line > 0:
                lines_map.setdefault(line, []).append(player_data)
            else:
                extras.append(player_data)

        # Sort goalies by line number (starter first, preserving API order for ties)
        goalies.sort(key=lambda g: (g.get("line") or 999,))

        # Mark the first goalie as the starting goalkeeper
        for i, g in enumerate(goalies):
            g["isStarter"] = (i == 0)

        # Build structured lines
        for line_num in sorted(lines_map.keys()):
            line_players = lines_map[line_num]
            forwards = [p for p in line_players if p["role"] in FORWARD_ROLES]
            defensemen = [p for p in line_players if p["role"] in DEFENSE_ROLES]

            # Sort forwards: LW, C, RW
            role_order = {"LEFT_WING": 0, "CENTER": 1, "RIGHT_WING": 2}
            forwards.sort(key=lambda p: role_order.get(p["role"], 9))

            # Sort defensemen: LD, RD
            def_order = {"LEFT_DEFENSEMAN": 0, "DEFENSEMAN": 1, "RIGHT_DEFENSEMAN": 2}
            defensemen.sort(key=lambda p: def_order.get(p["role"], 9))

            team_data["lines"].append({
                "lineNum": line_num,
                "forwards": forwards,
                "defensemen": defensemen,
            })

        team_data["goalies"] = goalies
        team_data["extras"] = extras

        # Determine best U20 skater per team via Red Bull award.
        # If no Red Bull award, fall back to highest-scoring U20 skater.
        if not redbull_ids:
            all_skaters: list[dict] = []
            for line_data in team_data["lines"]:
                all_skaters.extend(line_data["forwards"])
                all_skaters.extend(line_data["defensemen"])
            all_skaters.extend(team_data["extras"])

            u20_skaters = [p for p in all_skaters if p.get("isU20")]
            if u20_skaters:
                u20_skaters.sort(key=lambda p: (-p["points"], -p["goals"]))
                u20_skaters[0]["isBestU20"] = True

        result[side] = team_data

    return result


def _title(name: str) -> str:
    """Convert 'MIRO' to 'Miro'."""
    if not name:
        return name
    return name.title()


def _format_toi(seconds: float) -> str:
    """Format time-on-ice from seconds to MM:SS."""
    if not seconds:
        return "0:00"
    minutes = int(seconds) // 60
    secs = int(seconds) % 60
    return f"{minutes}:{secs:02d}"
