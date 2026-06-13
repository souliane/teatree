"""The pane-reaper mini-loop builds its global scanner job when teams is on (#1838 PR#7b).

Mirrors ``src/teatree/loops/pane_reaper/loop.py``. ``build_jobs`` returns one
global ``_ScannerJob`` when the factory yields a scanner (``teams_enabled``), and
an empty list when teams is off (the factory returns ``None``) — DEFAULT-OFF.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.loops.pane_reaper.loop import MINI_LOOP as PANE_REAPER_LOOP


class TestPaneReaperMiniLoop(TestCase):
    def test_name_and_cadence(self) -> None:
        assert PANE_REAPER_LOOP.name == "pane_reaper"
        assert PANE_REAPER_LOOP.default_cadence_seconds == 300

    def test_build_jobs_wires_scanner_when_enabled(self) -> None:
        from teatree.loop.scanners import PaneReaperScanner  # noqa: PLC0415

        fake = PaneReaperScanner(teams_enabled=True, idle_minutes=30)
        with patch(
            "teatree.loop.global_scanner_factories._pane_reaper_scanner",
            return_value=fake,
        ):
            jobs = PANE_REAPER_LOOP.build_jobs()
        assert len(jobs) == 1
        assert jobs[0].scanner is fake
        assert jobs[0].overlay == ""

    def test_build_jobs_empty_when_disabled(self) -> None:
        with patch(
            "teatree.loop.global_scanner_factories._pane_reaper_scanner",
            return_value=None,
        ):
            assert PANE_REAPER_LOOP.build_jobs() == []
