"""Client for the Liiga.fi API v2."""

import asyncio
import httpx
import time
from datetime import date, datetime
from typing import Any

LIIGA_API_BASE = "https://liiga.fi/api/v2"
TIMEOUT = 30.0

# Simple TTL cache for season stats (they don't change often)
_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 300  # 5 minutes
CACHE_TTL_LONG = 3600  # 1 hour – for data that rarely changes


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


async def get_games_for_date(game_date: date, tournament: str = "runkosarja") -> dict:
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


async def get_season_stats(season: int, tournament: str = "runkosarja") -> list[dict]:
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

    async def _fetch_one(pid: int) -> tuple[int, dict | None]:
        async with sem:
            try:
                data = await get_player_game_log(pid, season)
                regular = data.get("regular", []) if isinstance(data, dict) else []
                for entry in regular:
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
) -> dict:
    """Build a structured roster view for a single game.

    ``match_stats_map`` is an optional mapping of
    ``{player_id: {goals, assists, points, penaltyMinutes, plusMinus}}``
    containing the per-game stats for this match.  When provided the
    season totals shown will be *adjusted* (season minus match).
    """
    if match_stats_map is None:
        match_stats_map = {}

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
                "goals": s_goals - m_goals,
                "assists": s_assists - m_assists,
                "points": s_points - m_points,
                "plusMinus": s_pm - m_pm,
                "penaltyMinutes": s_pim - m_pim,
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
