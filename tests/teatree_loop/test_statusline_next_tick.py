"""Statusline renderer refit — consolidated loop line + state-priority reorder.

Three behaviours regression-locked here:

*   Line 1 of the statusline is the **consolidated loop summary**, not a
    per-loop dump (``loop:owner``, ``loop:self-improve``, ``loop:tick``).
    The user explicitly asked for "time to next tick" on the first line.
*   Anchor state groups render in priority order — actively-shipping work
    first (``started``, ``in_review``, ``ready``) before the long
    ``not_started`` backlog. A 41-deep ``not_started`` no longer pushes
    the actionable rows off-screen.
*   The ``not_started`` cap tightens to 3 with a clear ``(+N more)``
    overflow marker; ``ready:`` and the rest keep the standard
    ``_MAX_PER_STATE`` cap with the same overflow wording.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from teatree.loop.dispatch import DispatchAction
from teatree.loop.rendering import zones_for
from teatree.loop.statusline import live_loops_anchor, mini_loops_anchor, render
from teatree.loop.statusline_loops import _mini_loop_chunk
from teatree.loop.statusline_render import _format_duration


def _active_ticket(num: str, state: str, *, url: str = "", overlay: str = "ov") -> DispatchAction:
    return DispatchAction(
        kind="statusline",
        zone="anchors",
        detail=f"#{num} {state}",
        payload={
            "ticket_number": num,
            "state": state,
            "issue_url": url or f"https://example.com/issues/{num}",
            "overlay": overlay,
        },
    )


def _ready(num: str, *, overlay: str = "ov") -> DispatchAction:
    return DispatchAction(
        kind="statusline",
        zone="action_needed",
        detail=f"Ready to start: #{num}",
        payload={
            "url": f"https://example.com/issues/{num}",
            "ticket_number": num,
            "overlay": overlay,
        },
    )


class TestConsolidatedLoopAnchor:
    """Line 1 = ``<name> <Nm> · <name> <Nm>`` (per-loop relative ticks)."""

    def test_includes_relative_minutes_when_acquired_at_known(self) -> None:
        # Each lease carries its own acquire instant; 2 minutes elapsed of
        # the 720s cadence → next tick in 10m. The t3-master lease is
        # excluded from the shared loop line — its badge is per-session in
        # statusline.sh instead.
        acquired_at = datetime.now(UTC) - timedelta(seconds=120)
        leases = [("loop-tick", acquired_at), ("t3-master", acquired_at)]
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
        ):
            lines = live_loops_anchor()
        assert len(lines) == 1, repr(lines)
        line = lines[0]
        assert line.startswith("tick "), line
        # Per-loop name + relative minutes, no headline count.
        assert "loops live" not in line, line
        assert "tick 10m" in line, line
        # t3-master is excluded from the shared line (per-session badge in sh).
        assert "owner" not in line, line

    def test_names_only_when_no_lease_history(self) -> None:
        leases = [("loop-tick", None)]
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch("teatree.loop.statusline_loops._availability_segment", return_value=""),
        ):
            lines = live_loops_anchor()
        assert lines == ["tick"], repr(lines)

    def test_reports_due_when_overdue(self) -> None:
        # Acquired 1 hour ago; cadence 12 minutes → due now.
        acquired_at = datetime.now(UTC) - timedelta(hours=1)
        leases = [("loop-tick", acquired_at)]
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch("teatree.loop.statusline_loops._availability_segment", return_value=""),
        ):
            lines = live_loops_anchor()
        assert lines == ["tick due"], repr(lines)

    def test_no_per_loop_lines_anymore(self) -> None:
        """The pre-refit one-line-per-loop shape is gone (user explicitly opted out)."""
        leases = [("loop-tick", None), ("t3-master", None), ("loop-self-improve", None)]
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
        ):
            lines = live_loops_anchor()
        assert len(lines) == 1, repr(lines)
        # No ``loop:owner`` / ``loop:tick`` / ``loop:self-improve`` tokens.
        assert "loop:owner" not in lines[0], lines[0]
        assert "loop:tick" not in lines[0], lines[0]
        assert "loop:self-improve" not in lines[0], lines[0]

    def test_empty_when_no_loops_live(self) -> None:
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[]),
            patch("teatree.loop.statusline_loops._mini_loop_schedules", return_value=[]),
        ):
            assert live_loops_anchor() == []

    def test_fails_open_on_db_error(self) -> None:
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", side_effect=RuntimeError("db down")),
            patch("teatree.loop.statusline_loops._mini_loop_schedules", return_value=[]),
        ):
            assert live_loops_anchor() == []


class TestMiniLoopsAnchor:
    """Every enabled domain mini-loop renders its own next-tick countdown.

    The line must show ALL active crons, not just the infra leases, and
    each countdown must be DERIVED from that loop's own ``next_fire_at`` so
    it counts down across renders rather than freezing on a constant.
    """

    def test_one_chunk_per_enabled_mini_loop_with_own_countdown(self) -> None:
        now = datetime.now(UTC)
        schedules = [
            ("dispatch", now + timedelta(seconds=120), 600),
            ("tickets", now + timedelta(seconds=240), 600),
            ("news", now + timedelta(seconds=18 * 60), 3600),
        ]
        with patch("teatree.loop.statusline_loops._mini_loop_schedules", return_value=schedules):
            chunks = mini_loops_anchor()
        # Each loop carries its OWN countdown, not a shared value.
        assert chunks == ["dispatch 2m", "tickets 4m", "news 18m"], chunks

    def test_never_fired_loop_reads_due(self) -> None:
        with patch("teatree.loop.statusline_loops._mini_loop_schedules", return_value=[("inbox", None, 300)]):
            assert mini_loops_anchor() == ["inbox due"]

    def test_overdue_loop_reads_due(self) -> None:
        past = datetime.now(UTC) - timedelta(minutes=5)
        with patch("teatree.loop.statusline_loops._mini_loop_schedules", return_value=[("review", past, 300)]):
            assert mini_loops_anchor() == ["review due"]

    def test_empty_when_no_mini_loops_enabled(self) -> None:
        with patch("teatree.loop.statusline_loops._mini_loop_schedules", return_value=[]):
            assert mini_loops_anchor() == []

    def test_fails_open_on_db_error(self) -> None:
        with patch("teatree.loop.statusline_loops._mini_loop_schedules", side_effect=RuntimeError("db down")):
            assert mini_loops_anchor() == []

    def test_chunk_countdown_is_relative_to_now_not_static(self) -> None:
        # A nearer next-fire instant renders a SMALLER countdown than a
        # farther one — proving the value is derived from (next_fire - now),
        # not a cached constant.
        now = datetime.now(UTC)
        with patch(
            "teatree.loop.statusline_loops._mini_loop_schedules",
            return_value=[("ship", now + timedelta(seconds=600), 1200)],
        ):
            far = mini_loops_anchor()
        with patch(
            "teatree.loop.statusline_loops._mini_loop_schedules",
            return_value=[("ship", now + timedelta(seconds=120), 1200)],
        ):
            near = mini_loops_anchor()
        assert far == ["ship 10m"], far
        assert near == ["ship 2m"], near


class TestLoopLineComposesLeasesAndMiniLoops:
    """The single loop line lists infra leases AND mini-loops."""

    def test_both_sources_appear_on_one_line(self) -> None:
        acquired_at = datetime.now(UTC) - timedelta(seconds=120)
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[("loop-tick", acquired_at)]),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch(
                "teatree.loop.statusline_loops._mini_loop_schedules",
                return_value=[("dispatch", datetime.now(UTC) + timedelta(seconds=120), 600)],
            ),
            patch("teatree.loop.statusline_loops._availability_segment", return_value=""),
        ):
            lines = live_loops_anchor(colorize=False)
        assert lines == ["tick 10m · dispatch 2m"], lines

    def test_line_renders_for_mini_loops_even_with_no_live_lease(self) -> None:
        # The user's complaint: crons were invisible when no infra lease was
        # live. A mini-loop alone now surfaces the loop line.
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[]),
            patch(
                "teatree.loop.statusline_loops._mini_loop_schedules",
                return_value=[("resource_pressure", datetime.now(UTC) + timedelta(seconds=60), 600)],
            ),
            patch("teatree.loop.statusline_loops._availability_segment", return_value=""),
        ):
            lines = live_loops_anchor(colorize=False)
        assert lines == ["resource_pressure 1m"], lines


class TestMiniLoopChunk:
    """The pure ``<name> <next-tick>`` formatter."""

    def test_none_next_fire_is_due(self) -> None:
        assert _mini_loop_chunk("audit", None) == "audit due"

    def test_future_next_fire_is_relative_minutes(self) -> None:
        assert _mini_loop_chunk("audit", datetime.now(UTC) + timedelta(seconds=300)) == "audit 5m"


class TestFormatDuration:
    """Pure helper — covered for completeness so the shape is locked."""

    def test_seconds_only(self) -> None:
        assert _format_duration(45) == "45s"

    def test_minutes_and_seconds(self) -> None:
        assert _format_duration(192) == "3m12s"

    def test_whole_minutes(self) -> None:
        assert _format_duration(120) == "2m"

    def test_hours_and_minutes(self) -> None:
        assert _format_duration(3900) == "1h05m"


class TestAnchorStatePriorityOrder:
    """Anchor state groups render in priority order, not insertion order."""

    def test_started_renders_before_coded(self, tmp_path: Path) -> None:
        # With ``not_started`` and ``in_review`` filtered out of the anchor
        # row (#1377), priority is asserted on the surviving
        # actively-shipping states: ``started`` before ``coded``.
        actions = [
            _active_ticket("100", "coded", overlay="ov"),
            _active_ticket("200", "started", overlay="ov"),
        ]
        with patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[]):
            zones = zones_for(actions, colorize=False)
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        # Terse format has no ``state:`` labels — assert on item order.
        idx_200 = body.index("#200")
        idx_100 = body.index("#100")
        assert idx_200 < idx_100, body


class TestActiveStateOverflowCap:
    """Active-state items cap at 5 with ``(+N more)`` overflow phrasing."""

    def test_started_caps_at_five(self, tmp_path: Path) -> None:
        actions = [_active_ticket(str(i), "started", overlay="ov") for i in range(1, 11)]
        with patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[]):
            zones = zones_for(actions, colorize=False)
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        # First five IDs visible, 5 overflowed.
        assert "#1" in body
        assert "#5" in body
        assert "(+5 more)" in body, body


class TestReadyOverflowPhrasing:
    """The action_needed ``ready:`` row uses the same ``(+N more)`` overflow shape."""

    def test_ready_overflow_says_more(self, tmp_path: Path) -> None:
        actions = [_ready(str(i), overlay="ov") for i in range(10)]
        with patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[]):
            zones = zones_for(actions, colorize=False)
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        assert "(+5 more)" in body, body


class TestZonesForIntegration:
    """End-to-end: ``zones_for`` + ``render`` produces the new top-line shape."""

    def test_line_one_is_per_loop_summary(self, tmp_path: Path) -> None:
        acquired_at = datetime.now(UTC) - timedelta(seconds=60)
        # The t3-master lease is present but excluded from the shared line
        # (it is rendered as a per-session badge in statusline.sh instead).
        leases = [("loop-tick", acquired_at), ("t3-master", acquired_at)]
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
        ):
            zones = zones_for([], colorize=False)
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        first_line = body.splitlines()[0]
        assert first_line.startswith("tick "), repr(first_line)
        # 60s elapsed of 720s → next tick in 11m; loop-tick appears, t3-master absent.
        assert "tick 11m" in first_line, first_line
        assert "owner" not in first_line, first_line
        assert "loops live" not in first_line, first_line
        # Per-loop tokens removed at the top.
        assert "\nloop:tick" not in body
        assert "\nloop:owner" not in body
