"""Client for the Liiga.fi API v2."""

import httpx
import time
from datetime import date, datetime
from typing import Any

LIIGA_API_BASE = "https://liiga.fi/api/v2"
TIMEOUT = 30.0

# Simple TTL cache for season stats (they don't change often)
_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 300  # 5 minutes


async def _get(path: str, params: dict | None = None) -> Any:
    """Make a GET request to the Liiga API."""
    url = f"{LIIGA_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


async def _get_cached(cache_key: str, path: str, params: dict | None = None) -> Any:
    """GET with in-memory TTL caching."""
    now = time.time()
    if cache_key in _cache:
        ts, data = _cache[cache_key]
        if now - ts < CACHE_TTL:
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
        # Season 2026 means 2025-26, so season start year = season - 1 = 2025
        # U20: born in (season_start_year - 19) or later = 2006+
        season_start_year = season - 1
        cutoff_year = season_start_year - 19
        return dob.year >= cutoff_year
    except (ValueError, TypeError):
        return False


# Mapping of role codes from the roster API to display groups
FORWARD_ROLES = {"LEFT_WING", "RIGHT_WING", "CENTER"}
DEFENSE_ROLES = {"LEFT_DEFENSEMAN", "RIGHT_DEFENSEMAN", "DEFENSEMAN"}
GOALIE_ROLES = {"GOALIE"}

# Short role labels for display
ROLE_SHORT = {
    "LEFT_WING": "LW",
    "RIGHT_WING": "RW",
    "CENTER": "C",
    "LEFT_DEFENSEMAN": "LD",
    "RIGHT_DEFENSEMAN": "RD",
    "DEFENSEMAN": "D",
    "GOALIE": "G",
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


def build_roster_view(game_detail: dict, season_stats_map: dict, season: int) -> dict:
    """Build a structured roster view for a single game.

    Returns a dict with homeTeam and awayTeam, each containing:
    - teamName, teamId, logos
    - lines: list of {lineNum, forwards: [...], defensemen: [...]}
    - goalies: list of goalie dicts
    - score info
    """
    result = {"game": {}}

    game = game_detail.get("game", {})
    awards = game_detail.get("awards", [])

    # Build award lookup: playerId -> list of award names
    award_map: dict[int, list[str]] = {}
    for aw in awards:
        pid = aw.get("playerId")
        if pid:
            award_map.setdefault(pid, []).append(aw.get("awardName", ""))

    # Find golden helmet players from awards
    golden_helmet_ids = set()
    for aw in awards:
        if "kultainen kypärä" in (aw.get("awardName", "")).lower():
            golden_helmet_ids.add(aw.get("playerId"))

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

        team_data = {
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
        goalies = []
        extras = []

        for p in players:
            line = p.get("line")
            role = p.get("role", "")
            player_id = p.get("id")

            # Look up season stats
            stats = season_stats_map.get(player_id, {})

            player_data = {
                "id": player_id,
                "firstName": _title(p.get("firstName", "")),
                "lastName": _title(p.get("lastName", "")),
                "jersey": p.get("jersey"),
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
                "isGoldenHelmet": player_id in golden_helmet_ids,
                "nationality": p.get("nationality", ""),
                # Season stats for skaters
                "games": stats.get("games", 0),
                "goals": stats.get("goals", 0),
                "assists": stats.get("assists", 0),
                "points": stats.get("points", 0),
                "plusMinus": stats.get("plusMinus", 0),
                "penaltyMinutes": stats.get("penaltyMinutes", 0),
                "powerplayGoals": stats.get("powerplayGoals", 0),
                "shots": stats.get("shots", 0),
                "shotPercentage": stats.get("shotPercentage", 0),
                "timeOnIceAvg": _format_toi(stats.get("timeOnIceAvg", 0)),
                # Goalie stats
                "savePercentage": stats.get("savePercentage", 0),
                "goalsAgainstAvg": stats.get("goalsAgainstAvg", 0),
                "gkWins": stats.get("gkWins", 0),
                "gkLosses": stats.get("gkLosses", 0),
                "gkTies": stats.get("gkTies", 0),
                "shutOut": stats.get("shutOut", 0),
            }

            if role in GOALIE_ROLES:
                goalies.append(player_data)
            elif line and line > 0:
                lines_map.setdefault(line, []).append(player_data)
            else:
                extras.append(player_data)

        # Sort goalies: starting goalie first (line 1 or 2 = starting)
        goalies.sort(key=lambda g: (g.get("games", 0) == 0, 0))

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
