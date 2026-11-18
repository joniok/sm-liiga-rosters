"""Offline checks for roster grouping and season-stat adjustments.

Match stats are built in the test. A row with ``played: True`` stands in for
a game-log entry, so nothing here touches the network.
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import liiga_client

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ARMALIS_ID = 61011727
SIDES = ("homeTeam", "awayTeam")
LINE_FORWARD_ROLES = {"LEFT_WING", "CENTER", "RIGHT_WING"}
YOUNG_BIRTH_YEAR = 2007  # season 2027 U20 fallback: born this year or later


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


def _stats_map(rows: list[dict]) -> dict[int, dict]:
    mapped: dict[int, dict] = {}
    for row in rows:
        player_id = row.get("playerId")
        if player_id:
            mapped[int(player_id)] = row
    return mapped


def _view(detail: dict, stats: dict, match: dict | None = None) -> dict:
    season = detail["game"]["season"]
    if match is None:
        return liiga_client.build_roster_view(
            detail,
            stats,
            season,
            stats_tournament=liiga_client.TOURNAMENT_REGULAR,
        )
    return liiga_client.build_roster_view(
        detail,
        stats,
        season,
        match,
        stats_tournament=liiga_client.TOURNAMENT_REGULAR,
    )


def _raw_players(detail: dict, side: str) -> list[dict]:
    key = "homeTeamPlayers" if side == "homeTeam" else "awayTeamPlayers"
    return list(detail.get(key) or [])


def _collect(team: dict) -> dict[str, list[dict]]:
    line_forwards: list[dict] = []
    line_defense: list[dict] = []
    for line in team.get("lines") or []:
        line_forwards.extend(line.get("forwards") or [])
        line_defense.extend(line.get("defensemen") or [])
    dressed = team.get("dressedExtras") or {}
    scratches = team.get("scratches") or {}
    return {
        "lines_f": line_forwards,
        "lines_d": line_defense,
        "dressed_f": list(dressed.get("forwards") or []),
        "dressed_d": list(dressed.get("defensemen") or []),
        "scratch_f": list(scratches.get("forwards") or []),
        "scratch_d": list(scratches.get("defensemen") or []),
        "scratch_g": list(scratches.get("goalies") or []),
        "goalies": list(team.get("goalies") or []),
    }


def _find(view: dict, player_id: int) -> dict:
    matches: list[dict] = []
    for side in SIDES:
        buckets = _collect(view[side])
        for players in buckets.values():
            matches.extend(player for player in players if player.get("id") == player_id)
    if len(matches) != 1:
        raise AssertionError(f"player {player_id} found {len(matches)} times in the roster view")
    return matches[0]


def _blank_match(**extra) -> dict:
    row = {
        "goals": 0,
        "assists": 0,
        "points": 0,
        "penaltyMinutes": 0,
        "plusMinus": 0,
        "played": True,
    }
    row.update(extra)
    return row


class RosterViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.played = _load("game_2701526_played.json")
        cls.upcoming = _load("game_2701456_upcoming.json")
        cls.stats = _stats_map(_load("season_stats_2027.json"))
        cls.games = (
            ("played", cls.played),
            ("upcoming", cls.upcoming),
        )

    def test_played_fourth_line_has_three_forwards_and_no_defensemen(self) -> None:
        view = _view(self.played, self.stats)
        for side in SIDES:
            fourth = [
                line for line in view[side].get("lines") or []
                if line.get("lineNum") == 4
            ]
            self.assertEqual(len(fourth), 1, f"{side} is missing line 4")
            self.assertEqual(len(fourth[0].get("forwards") or []), 3, side)
            self.assertEqual(len(fourth[0].get("defensemen") or []), 0, side)

    def test_seventh_and_eighth_defensemen_are_dressed_extras(self) -> None:
        saw_seventh = False
        saw_eighth = False
        for label, detail in self.games:
            view = _view(detail, self.stats)
            for side in SIDES:
                buckets = _collect(view[side])
                in_lines = [
                    player for player in buckets["lines_f"] + buckets["lines_d"]
                    if player.get("roleShort") in {"7.P", "8.P"}
                ]
                self.assertEqual(in_lines, [], f"{label} {side} keeps 7.P/8.P on a line")
                for player in _raw_players(detail, side):
                    role = player.get("role")
                    if role == "SEVENTH_DEFENSEMAN":
                        short = "7.P"
                        saw_seventh = True
                    elif role == "EIGHTH_DEFENSEMAN":
                        short = "8.P"
                        saw_eighth = True
                    else:
                        continue
                    placed = [
                        item for item in buckets["dressed_d"]
                        if item.get("id") == player["id"]
                    ]
                    self.assertEqual(
                        len(placed), 1,
                        f"{label} {side} {player['id']} should be a dressed extra defenseman",
                    )
                    self.assertEqual(placed[0].get("roleShort"), short)
                    for bucket_name in (
                        "lines_f", "lines_d", "dressed_f",
                        "scratch_f", "scratch_d", "scratch_g", "goalies",
                    ):
                        leaked = [
                            item for item in buckets[bucket_name]
                            if item.get("id") == player["id"]
                        ]
                        self.assertEqual(leaked, [], f"{label} {side} {short} also in {bucket_name}")
        self.assertTrue(saw_seventh)
        self.assertTrue(saw_eighth)

    def test_thirteenth_forward_is_only_a_dressed_extra(self) -> None:
        saw = False
        for label, detail in self.games:
            view = _view(detail, self.stats)
            for side in SIDES:
                buckets = _collect(view[side])
                for player in _raw_players(detail, side):
                    if player.get("role") != "THIRTEENTH_STRIKER":
                        continue
                    saw = True
                    placed = [
                        item for item in buckets["dressed_f"]
                        if item.get("id") == player["id"]
                    ]
                    self.assertEqual(len(placed), 1, f"{label} {side} 13.H missing from dressed extras")
                    self.assertEqual(placed[0].get("roleShort"), "13.H")
                    for bucket_name in (
                        "lines_f", "lines_d", "dressed_d",
                        "scratch_f", "scratch_d", "scratch_g", "goalies",
                    ):
                        leaked = [
                            item for item in buckets[bucket_name]
                            if item.get("id") == player["id"]
                        ]
                        self.assertEqual(leaked, [], f"{label} {side} 13.H also in {bucket_name}")
        self.assertTrue(saw, "fixtures contain no 13.H")

    def test_unlined_skaters_are_scratches_only(self) -> None:
        saw_forward = False
        saw_defense = False
        for label, detail in self.games:
            view = _view(detail, self.stats)
            for side in SIDES:
                buckets = _collect(view[side])
                for player in _raw_players(detail, side):
                    if player.get("line") is not None:
                        continue
                    role = player.get("role")
                    if role == "STRIKER":
                        home_bucket = "scratch_f"
                        saw_forward = True
                    elif role == "DEFENSEMAN":
                        home_bucket = "scratch_d"
                        saw_defense = True
                    else:
                        continue
                    placed = [
                        item for item in buckets[home_bucket]
                        if item.get("id") == player["id"]
                    ]
                    self.assertEqual(
                        len(placed), 1,
                        f"{label} {side} {role} {player['id']} missing from scratches",
                    )
                    for bucket_name in (
                        "lines_f", "lines_d", "dressed_f", "dressed_d", "goalies",
                    ):
                        leaked = [
                            item for item in buckets[bucket_name]
                            if item.get("id") == player["id"]
                        ]
                        self.assertEqual(
                            leaked, [],
                            f"{label} {side} {role} {player['id']} leaked into {bucket_name}",
                        )
        self.assertTrue(saw_forward)
        self.assertTrue(saw_defense)

    def test_scratch_goalies_are_not_listed_as_goalies(self) -> None:
        saw = False
        for label, detail in self.games:
            view = _view(detail, self.stats)
            for side in SIDES:
                buckets = _collect(view[side])
                for player in _raw_players(detail, side):
                    if player.get("role") != "GOALIE" or player.get("line") is not None:
                        continue
                    saw = True
                    in_scratches = [
                        item for item in buckets["scratch_g"]
                        if item.get("id") == player["id"]
                    ]
                    in_goalies = [
                        item for item in buckets["goalies"]
                        if item.get("id") == player["id"]
                    ]
                    self.assertEqual(len(in_scratches), 1, f"{label} {side} scratch goalie missing")
                    self.assertEqual(in_goalies, [], f"{label} {side} scratch goalie listed with goalies")
        self.assertTrue(saw, "fixtures contain no scratch goalie")

    def test_starter_is_the_line_one_goalie(self) -> None:
        for label, detail in self.games:
            view = _view(detail, self.stats)
            for side in SIDES:
                raw_starters = [
                    player for player in _raw_players(detail, side)
                    if player.get("role") == "GOALIE" and player.get("line") == 1
                ]
                self.assertEqual(len(raw_starters), 1, f"{label} {side} line-1 goalie")
                starters = [
                    goalie for goalie in (view[side].get("goalies") or [])
                    if goalie.get("isStarter")
                ]
                self.assertEqual(
                    [goalie.get("id") for goalie in starters],
                    [raw_starters[0]["id"]],
                    f"{label} {side} starter",
                )

    def test_has_lines_for_both_fixtures(self) -> None:
        for _label, detail in self.games:
            view = _view(detail, self.stats)
            self.assertIs(view.get("hasLines"), True)

    def test_upcoming_game_does_not_subtract_season_goals(self) -> None:
        chosen = None
        for side in SIDES:
            for player in _raw_players(self.upcoming, side):
                if player.get("role") not in LINE_FORWARD_ROLES:
                    continue
                row = self.stats.get(player["id"]) or {}
                if (row.get("goals") or 0) > 0:
                    chosen = (side, player, row)
                    break
            if chosen:
                break
        self.assertIsNotNone(chosen, "upcoming fixture has no skater with season goals")
        _side, player, row = chosen
        view = _view(self.upcoming, self.stats)
        shown = _find(view, player["id"])
        self.assertEqual(shown.get("goals"), row.get("goals"))
        self.assertEqual(shown.get("assists"), row.get("assists"))
        self.assertEqual(shown.get("points"), row.get("points"))
        self.assertEqual(shown.get("games"), row.get("games"))

    def test_played_game_subtracts_one_game_from_scoring_skater(self) -> None:
        chosen = None
        for side in SIDES:
            for player in _raw_players(self.played, side):
                if player.get("role") not in LINE_FORWARD_ROLES:
                    continue
                row = self.stats.get(player["id"]) or {}
                if (row.get("goals") or 0) > 0 and (row.get("games") or 0) > 0:
                    chosen = (player, row)
                    break
            if chosen:
                break
        self.assertIsNotNone(chosen, "played fixture has no scoring skater in season stats")
        player, row = chosen
        match = {
            player["id"]: _blank_match(goals=1, assists=0, points=1, played=True),
        }
        view = _view(self.played, self.stats, match)
        shown = _find(view, player["id"])
        self.assertGreater(shown.get("matchGoals") or 0, 0)
        self.assertEqual(shown.get("games"), row["games"] - 1)

    def test_armalis_games_follow_played_games_and_game_log(self) -> None:
        arm_row = self.stats[ARMALIS_ID]
        self.assertIn("playedGames", arm_row)
        side = next(
            name for name in SIDES
            if any(player.get("id") == ARMALIS_ID for player in _raw_players(self.played, name))
        )
        backup = next(
            player for player in _raw_players(self.played, side)
            if player.get("role") == "GOALIE" and player.get("line") == 2
        )
        backup_row = self.stats[backup["id"]]
        # Armalis has a game-log stand-in; the backup does not.
        match = {ARMALIS_ID: _blank_match(played=True)}
        has_log = bool(match[ARMALIS_ID].get("played"))
        expected = arm_row["playedGames"] - (1 if has_log else 0)
        view = _view(self.played, self.stats, match)
        shown = _find(view, ARMALIS_ID)
        self.assertEqual(shown.get("games"), expected)
        backup_shown = _find(view, backup["id"])
        self.assertEqual(backup_shown.get("games"), backup_row["playedGames"])

    def test_is_u20_comes_from_stats_when_present(self) -> None:
        young_id = None
        older_id = None
        for side in SIDES:
            for player in _raw_players(self.played, side):
                row = self.stats.get(player["id"]) or {}
                birth = player.get("dateOfBirth") or ""
                if "isU20" not in row or len(birth) < 4:
                    continue
                year = int(birth[:4])
                if row["isU20"] and year >= YOUNG_BIRTH_YEAR and young_id is None:
                    young_id = player["id"]
                elif (not row["isU20"]) and year < YOUNG_BIRTH_YEAR and older_id is None:
                    older_id = player["id"]
        self.assertIsNotNone(young_id, "no stats isU20 player born in the U20 window")
        self.assertIsNotNone(older_id, "no older player with isU20 false")

        mutated = copy.deepcopy(self.stats)
        mutated[young_id]["isU20"] = False
        mutated[older_id]["isU20"] = True
        view = _view(self.played, mutated)
        self.assertEqual(_find(view, young_id).get("isU20"), False)
        self.assertEqual(_find(view, older_id).get("isU20"), True)

    def test_goalie_record_adjusted_when_match_map_has_result(self) -> None:
        """W-T-L moves when the match map carries gkWon/gkTied/gkLost."""
        home_id = 910001
        away_id = 910002
        detail = {
            "game": {
                "id": 999001,
                "season": 2027,
                "serie": "RUNKOSARJA",
                "started": True,
                "ended": True,
                "finishedType": "ENDED_DURING_REGULAR_GAME_TIME",
                "start": "2026-11-01T16:00:00.000Z",
                "homeTeam": {"teamName": "Koti", "teamId": "KOT", "goals": 3, "logos": {}},
                "awayTeam": {"teamName": "Vieras", "teamId": "VIE", "goals": 1, "logos": {}},
            },
            "awards": [],
            "homeTeamPlayers": [{
                "id": home_id,
                "firstName": "Aloitus",
                "lastName": "Vahti",
                "role": "GOALIE",
                "roleCode": "MV",
                "line": 1,
                "jersey": 30,
                "dateOfBirth": "1995-01-01",
            }],
            "awayTeamPlayers": [{
                "id": away_id,
                "firstName": "Toinen",
                "lastName": "Vahti",
                "role": "GOALIE",
                "roleCode": "MV",
                "line": 1,
                "jersey": 31,
                "dateOfBirth": "1996-01-01",
            }],
        }
        stats = {
            home_id: {
                "playerId": home_id,
                "games": 10,
                "playedGames": 8,
                "goals": 0,
                "assists": 0,
                "points": 0,
                "penaltyMinutes": 0,
                "plusMinus": 0,
                "gkWins": 5,
                "gkTies": 2,
                "gkLosses": 1,
                "isU20": False,
                "goalkeeper": True,
            },
            away_id: {
                "playerId": away_id,
                "games": 9,
                "playedGames": 7,
                "goals": 0,
                "assists": 0,
                "points": 0,
                "penaltyMinutes": 0,
                "plusMinus": 0,
                "gkWins": 3,
                "gkTies": 1,
                "gkLosses": 4,
                "isU20": False,
                "goalkeeper": True,
            },
        }
        match = {
            home_id: _blank_match(played=True, gkWon=1, gkTied=0, gkLost=0),
        }
        shown = _find(_view(detail, stats, match), home_id)
        self.assertEqual(shown.get("gkWins"), stats[home_id]["gkWins"] - match[home_id]["gkWon"])
        self.assertEqual(shown.get("gkTies"), stats[home_id]["gkTies"] - match[home_id]["gkTied"])
        self.assertEqual(shown.get("gkLosses"), stats[home_id]["gkLosses"] - match[home_id]["gkLost"])


if __name__ == "__main__":
    unittest.main()
