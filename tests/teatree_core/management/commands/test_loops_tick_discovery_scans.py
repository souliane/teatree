"""A loop tick scans the installed distributions for overlays once, not once per config read (#4999)."""

import importlib.metadata
import io
import tempfile
from pathlib import Path
from unittest.mock import patch

import django.test
from django.core.management import call_command

from teatree.core.models import Loop

_REAL_ENTRY_POINTS = importlib.metadata.entry_points
_OVERLAY_GROUP = "teatree.overlays"
_SCAN_BUDGET = 4  # one scan inside the tick's scope plus the few reads before it opens; 128 without the scope


class TestLoopTickOverlayScans(django.test.TestCase):
    def setUp(self) -> None:
        Loop.objects.create(name="probe", script="src/teatree/loops/probe/loop.py", delay_seconds=60)
        self.statusline = Path(self.enterContext(tempfile.TemporaryDirectory())) / "statusline"

    def _overlay_scans_during_a_tick(self) -> int:
        scans: list[dict[str, str]] = []

        def _counting(**kwargs: str) -> object:
            if kwargs.get("group") == _OVERLAY_GROUP:
                scans.append(kwargs)
            return _REAL_ENTRY_POINTS(**kwargs)

        with (
            patch("importlib.metadata.entry_points", side_effect=_counting),
            patch("teatree.loops.loop_table.dispatch_loop_table", return_value=[]),
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
        ):
            call_command(
                "loops_tick",
                loop="probe",
                statusline_file=self.statusline,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        return len(scans)

    def test_the_pipeline_scans_the_overlay_group_within_a_fixed_budget(self) -> None:
        assert self._overlay_scans_during_a_tick() <= _SCAN_BUDGET
