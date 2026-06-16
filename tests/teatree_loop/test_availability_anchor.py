"""Tests for the availability segment on the loop line (#58, #1678).

The loop line carries an ``availability: <present|away>
(<source>)`` segment reflecting the currently-resolved availability, read
live at render time. The label is deliberately distinct from the config
``Mode`` enum (auto/interactive) and from other ``mode=`` usages, and the
old standalone ``mode=away`` line is gone.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.core.availability import MODE_AWAY, MODE_PRESENT, Resolution, write_override
from teatree.loop.statusline import availability_segment, live_loops_anchor


class TestAvailabilitySegment:
    def test_present_segment_shows_explicit_label_and_source(self) -> None:
        assert availability_segment(Resolution(mode="present", source="default")) == "availability: present (default)"

    def test_away_segment_shows_explicit_label_and_source(self) -> None:
        assert availability_segment(Resolution(mode="away", source="schedule")) == "availability: away (schedule)"

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
        assert away_line.startswith("tick "), away_line
        assert "· availability: away (override)" in away_line, away_line

        write_override(MODE_PRESENT)
        present_line = self._loop_line()
        assert "· availability: present (override)" in present_line, present_line
        assert "away" not in present_line, present_line
