"""Tests for the availability anchor injected into the statusline (#58).

The anchor surfaces ``mode=away · N queued`` so the user sees both the
mode and the backlog depth from any terminal that consumes the
statusline.
"""

from teatree.loop.statusline import availability_anchor


class TestAvailabilityAnchor:
    def test_present_mode_is_empty_anchor(self) -> None:
        assert availability_anchor("present", 0) == ""

    def test_away_mode_with_no_queue(self) -> None:
        assert availability_anchor("away", 0) == "mode=away"

    def test_away_mode_with_queue(self) -> None:
        assert availability_anchor("away", 3) == "mode=away · 3 queued"

    def test_unknown_mode_is_empty(self) -> None:
        assert availability_anchor("???", 5) == ""
