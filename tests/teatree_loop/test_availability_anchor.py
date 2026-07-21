"""Tests for the availability segment on the loop line (#58, #1678, #3494).

The loop line carries an ``availability: <present|away>`` segment reflecting
the currently-resolved availability, read live at render time. The deciding
layer is intentionally not shown (the owner's spelled-out layout keeps the
segment to the bare state). The label is deliberately distinct from the config
``Mode`` enum (auto/interactive) and from other ``mode=`` usages, and the
old standalone ``mode=away`` line is gone.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.core.availability import MODE_AWAY, MODE_PRESENT, Resolution, write_override
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.waiting_item import WaitingItem
from teatree.loop.statusline import availability_segment, live_loops_anchor


class TestAvailabilitySegment:
    def test_present_segment_shows_explicit_label(self) -> None:
        assert availability_segment(Resolution(mode="present", source="default")) == "availability: present"

    def test_away_segment_shows_explicit_label(self) -> None:
        assert availability_segment(Resolution(mode="away", source="schedule")) == "availability: away"

    def test_label_is_unambiguous_not_bare_mode(self) -> None:
        segment = availability_segment(Resolution(mode="away", source="override"))
        assert segment.startswith("availability: ")
        assert "mode=" not in segment

    def test_unknown_mode_is_empty(self) -> None:
        assert availability_segment(Resolution(mode="???", source="default")) == ""


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestAvailabilitySegmentRidesLoopLineLive:
    @pytest.fixture
    def override_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        target = tmp_path / "availability_override.json"
        monkeypatch.setattr("teatree.core.availability.override_path", lambda: target)
        return target

    def _loop_line(self) -> str:
        acquired_at = datetime.now(UTC) - timedelta(seconds=120)
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[("loop-tick", acquired_at)]),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
        ):
            lines = live_loops_anchor()
        assert len(lines) == 1, lines
        return lines[0]

    def test_segment_tracks_a_live_away_then_present_flip(self, override_file: Path) -> None:
        write_override(MODE_AWAY)
        away_line = self._loop_line()
        # Infra leases sit at the tail; the availability segment reads the bare
        # state with no deciding-layer source.
        assert away_line.endswith("tick 10m"), away_line
        assert "availability: away" in away_line, away_line
        assert "(override)" not in away_line, away_line

        write_override(MODE_PRESENT)
        present_line = self._loop_line()
        assert "availability: present" in present_line, present_line
        assert "away" not in present_line, present_line


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestWaitingCountCoversAllKinds:
    """The loop-line ``N waiting`` counts the whole waiting-on-you lane (PR-21)."""

    def _loop_line(self) -> str:
        acquired_at = datetime.now(UTC) - timedelta(seconds=120)
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[("loop-tick", acquired_at)]),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch("teatree.loop.statusline_loops._availability_segment", return_value=""),
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
