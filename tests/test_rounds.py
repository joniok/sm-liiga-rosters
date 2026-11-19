"""Round context and template smoke tests. No network."""

from __future__ import annotations

import re
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import liiga_client
from app.rounds import build_context
from tests.test_roster_view import _stats_map, _view, _load

FI = ZoneInfo("Europe/Helsinki")


class RoundContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_static_build_hides_the_playoff_stats_toggle(self) -> None:
        today = datetime(2026, 11, 18, 12, tzinfo=FI)
        empty = {"games": [], "previousGameDate": None, "nextGameDate": None}
        with (
            patch.object(liiga_client, "use_playoffs_for_games", AsyncMock(return_value=True)),
            patch.object(liiga_client, "get_games_for_date", AsyncMock(return_value=empty)),
            patch.object(
                liiga_client,
                "get_playoff_series_board",
                AsyncMock(return_value=[{"phase": 5, "phase_label": "Finaalit", "series": []}]),
            ),
        ):
            static_ctx = await build_context(today.date(), today, static=True)
            app_ctx = await build_context(today.date(), today, stats_mode="runkosarja")

        self.assertFalse(static_ctx["show_stats_toggle"])
        self.assertTrue(app_ctx["show_stats_toggle"])
        self.assertEqual(app_ctx["stats_mode"], "runkosarja")
        self.assertEqual(static_ctx["playoff_series_phases"][0]["phase_label"], "Finaalit")
        self.assertEqual(static_ctx["prev_round_views"], [])
        self.assertEqual(static_ctx["next_round_views"], [])


class TemplateSmokeTests(unittest.TestCase):
    def test_fixture_roster_renders(self) -> None:
        detail = _load("game_2701526_played.json")
        stats = _stats_map(_load("season_stats_2027.json"))
        view = _view(detail, stats)
        env = Environment(loader=FileSystemLoader(str(ROOT / "app" / "templates")))
        html = env.get_template("index.html").render(
            today_fi="Keskiviikko 18.11.2026",
            has_games_today=True,
            prev_round_views=[view],
            prev_round_date_fi="Sunnuntai 15.11.2026",
            next_round_views=[],
            next_round_date_fi=None,
            next_round_is_today=False,
            show_stats_toggle=False,
            stats_mode="runkosarja",
            playoff_series_phases=[],
            build_time="test",
        )
        self.assertIn("Luke Henman", html)
        self.assertIn("xGE", html)
        self.assertIn("Lisäpelaajat", html)
        self.assertNotIn("None", html.split("player-name")[1][:40])
        summaries = re.findall(
            r'class="summary-games" title="Ottelut">(\d+)<span class="stat-label">O</span>',
            html,
        )
        self.assertTrue(summaries)
        self.assertNotIn(
            '<span class="summary-games" title="Ottelut"><span class="stat-label">O</span>',
            html,
        )


if __name__ == "__main__":
    unittest.main()
