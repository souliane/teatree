"""The loop line's merged ``mode:`` handle + waiting count (#58, #1678, #3494, #61).

The old separate ``availability:`` segment is GONE — reachability is
now intrinsic to the operating mode, so the loop line carries ONE ``mode:`` handle
(the collapse the owner asked for) and never a redundant second segment.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from teatree.core.models import ConfigSetting, Mode, ModeOverride
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.waiting_item import WaitingItem
from teatree.loop.statusline import live_loops_anchor, set_preset_line_reader
from teatree.loops.preset_status import preset_line_handles


def _reset_reader() -> None:
    set_preset_line_reader(None)


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestModeHandleRidesLoopLine:
    """The merged ``mode:`` handle rides the loop line; the second segment is gone."""

    def _loop_line(self) -> str:
        acquired_at = datetime.now(UTC) - timedelta(seconds=120)
        set_preset_line_reader(preset_line_handles)
        try:
            with (
                patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[("loop-tick", acquired_at)]),
                patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            ):
                lines = live_loops_anchor()
        finally:
            _reset_reader()
        assert len(lines) == 1, lines
        return lines[0]

    def test_manual_override_renders_mode_manual_never_a_second_segment(self) -> None:
        Mode.objects.update_or_create(name="off", defaults={"entries": {}})
        ModeOverride.objects.set_override("off")
        line = self._loop_line()
        assert "mode: manual" in line, line
        assert "availability:" not in line, line
        assert "preset:" not in line, line

    def test_default_mode_renders_its_name(self) -> None:
        Mode.objects.update_or_create(name="present", defaults={"entries": {}})
        ConfigSetting.objects.set_value("default_mode", "present")
        line = self._loop_line()
        assert "mode: present" in line, line
        assert "availability:" not in line, line


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestWaitingCountCoversAllKinds:
    """The loop-line ``N waiting`` counts the whole waiting-on-you lane (PR-21)."""

    def _loop_line(self) -> str:
        acquired_at = datetime.now(UTC) - timedelta(seconds=120)
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[("loop-tick", acquired_at)]),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
        ):
            lines = live_loops_anchor()
        assert len(lines) == 1, lines
        return lines[0]

    def test_hidden_at_zero(self) -> None:
        assert "waiting" not in self._loop_line()

    def test_counts_manual_and_question_kinds(self) -> None:
        WaitingItem.objects.add("chase finance")
        DeferredQuestion.record("deploy now?")
        # Two distinct kinds → the count is all-kinds, not questions-only.
        assert "2 waiting" in self._loop_line()
