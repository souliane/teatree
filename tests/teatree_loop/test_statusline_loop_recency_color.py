"""Per-loop recency coloring on the loop line — color tracks delay vs cadence.

Each per-loop chunk is colored by how close its next tick is, judged
RELATIVE to that loop's own cadence (so a 60s cron and a 1h cron are scored
on their own scale):

*   green  — just ticked / plenty of cadence remaining,
*   yellow — approaching its next tick,
*   red    — overdue or about to tick.

The mapping is a pure function (:func:`_loop_recency_color`); the integration
side asserts the colored chunks ride the rendered loop line.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from teatree.loop.statusline import live_loops_anchor
from teatree.loop.statusline_loops import _loop_recency_color
from teatree.loop.statusline_palette import _ANSI_GREEN, _ANSI_RED, _ANSI_YELLOW


class TestLoopRecencyColor:
    """Pure delay→color mapping, judged as a fraction of the loop's cadence."""

    def test_just_ticked_is_green(self) -> None:
        # Full cadence still ahead → plenty of time → green.
        assert _loop_recency_color(600, 600) == _ANSI_GREEN

    def test_more_than_half_cadence_remaining_is_green(self) -> None:
        assert _loop_recency_color(360, 600) == _ANSI_GREEN

    def test_approaching_is_yellow(self) -> None:
        # A fifth of the cadence left → approaching → yellow.
        assert _loop_recency_color(120, 600) == _ANSI_YELLOW

    def test_about_to_tick_is_red(self) -> None:
        # A sliver of the cadence left → imminent → red.
        assert _loop_recency_color(30, 600) == _ANSI_RED

    def test_due_now_is_red(self) -> None:
        assert _loop_recency_color(0, 600) == _ANSI_RED

    def test_overdue_is_red(self) -> None:
        assert _loop_recency_color(-120, 600) == _ANSI_RED

    def test_unknown_next_tick_is_red(self) -> None:
        # ``None`` seconds-until = no acquire / never fired → overdue → red.
        assert _loop_recency_color(None, 600) == _ANSI_RED

    def test_relative_to_cadence_not_absolute(self) -> None:
        # The SAME 120s-until reads differently per cadence: green when it is
        # half of a 240s cadence, red when it is a sliver of a 1000s cadence.
        assert _loop_recency_color(120, 240) == _ANSI_GREEN
        assert _loop_recency_color(120, 1000) == _ANSI_RED

    def test_zero_or_negative_cadence_is_red(self) -> None:
        # A non-positive cadence is meaningless — fail safe to red, never crash.
        assert _loop_recency_color(60, 0) == _ANSI_RED


class TestColoredChunksRideTheLoopLine:
    """When colorize is on, each per-loop chunk carries its recency color."""

    def test_imminent_lease_chunk_is_red(self) -> None:
        # Acquired 690s ago of a 720s cadence → 30s left → red.
        acquired_at = datetime.now(UTC) - timedelta(seconds=690)
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[("loop-tick", acquired_at)]),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch("teatree.loop.statusline_loops._availability_segment", return_value=""),
            patch("teatree.loop.statusline_loops._waiting_count", return_value=0),
        ):
            lines = live_loops_anchor(colorize=True)
        assert len(lines) == 1, lines
        assert _ANSI_RED in lines[0], repr(lines[0])

    def test_fresh_lease_chunk_is_green(self) -> None:
        # Acquired 30s ago of a 720s cadence → lots of headroom → green.
        acquired_at = datetime.now(UTC) - timedelta(seconds=30)
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[("loop-tick", acquired_at)]),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch("teatree.loop.statusline_loops._availability_segment", return_value=""),
            patch("teatree.loop.statusline_loops._waiting_count", return_value=0),
        ):
            lines = live_loops_anchor(colorize=True)
        assert _ANSI_GREEN in lines[0], repr(lines[0])

    def test_no_ansi_when_colorize_off(self) -> None:
        acquired_at = datetime.now(UTC) - timedelta(seconds=30)
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[("loop-tick", acquired_at)]),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch("teatree.loop.statusline_loops._availability_segment", return_value=""),
            patch("teatree.loop.statusline_loops._waiting_count", return_value=0),
        ):
            lines = live_loops_anchor(colorize=False)
        assert "\033[" not in lines[0], repr(lines[0])
        assert lines[0] == "tick 11m", repr(lines[0])

    def test_mini_loop_chunk_colored_relative_to_its_cadence(self) -> None:
        now = datetime.now(UTC)
        # dispatch: ~full 120s cadence ahead → plenty of time → green;
        # ship: 10s left of a 600s cadence → about to fire → red.
        schedules = [
            ("dispatch", now + timedelta(seconds=118), 120),
            ("ship", now + timedelta(seconds=10), 600),
        ]
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[]),
            patch("teatree.loop.statusline_loops._mini_loop_schedules", return_value=schedules),
            patch("teatree.loop.statusline_loops._availability_segment", return_value=""),
            patch("teatree.loop.statusline_loops._waiting_count", return_value=0),
        ):
            lines = live_loops_anchor(colorize=True)
        assert _ANSI_GREEN in lines[0], repr(lines[0])
        assert _ANSI_RED in lines[0], repr(lines[0])

    def test_never_fired_mini_loop_is_red(self) -> None:
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[]),
            patch("teatree.loop.statusline_loops._mini_loop_schedules", return_value=[("inbox", None, 300)]),
            patch("teatree.loop.statusline_loops._availability_segment", return_value=""),
            patch("teatree.loop.statusline_loops._waiting_count", return_value=0),
        ):
            lines = live_loops_anchor(colorize=True)
        assert _ANSI_RED in lines[0], repr(lines[0])
        # The text content is unchanged — color wraps it, never replaces it.
        assert "inbox due" in lines[0], repr(lines[0])

    def test_two_lease_segments_colored_independently(self) -> None:
        """PR-17 item 4: each live-loop segment carries its own color on one line."""
        now = datetime.now(UTC)
        leases = [
            ("loop-slack-answer", now - timedelta(seconds=690)),  # 30s left of 720 → red
            ("loop-self-improve", now - timedelta(seconds=30)),  # 690s left of 720 → green
        ]
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch("teatree.loop.statusline_loops._availability_segment", return_value=""),
            patch("teatree.loop.statusline_loops._waiting_count", return_value=0),
        ):
            lines = live_loops_anchor(colorize=True)
        assert len(lines) == 1, lines
        # Both colors appear on the single line — the two segments are NOT
        # painted one uniform color.
        assert _ANSI_RED in lines[0], repr(lines[0])
        assert _ANSI_GREEN in lines[0], repr(lines[0])
