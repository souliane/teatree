"""Statusline renderer refit — state-priority reorder + anchor cap shape.

Three behaviours regression-locked here:

*   Anchor state groups render in priority order — actively-shipping work
    first (``started``, ``in_review``, ``ready``) before the long
    ``not_started`` backlog. A 41-deep ``not_started`` no longer pushes
    the actionable rows off-screen.
*   The ``not_started`` cap tightens to 3 with a clear ``(+N more)``
    overflow marker; ``ready:`` and the rest keep the standard
    ``_MAX_PER_STATE`` cap with the same overflow wording.
*   The per-loop anchor lines (#1400) lead the anchors zone — one line
    per live loop with its own next-tick countdown.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from teatree.loop.dispatch import DispatchAction
from teatree.loop.rendering import zones_for
from teatree.loop.statusline import _format_duration, live_loops_anchor, render


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


class TestPerLoopAnchor:
    """One anchor line per live loop, with a per-loop next-tick countdown (#1400)."""

    def test_one_line_per_loop_with_next_tick(self) -> None:
        leases = [("loop-tick", "sessA"), ("loop-owner", "sessA")]
        now = datetime.now(UTC)
        acquired_ats = {
            "loop-tick": now - timedelta(seconds=120),
            "loop-owner": now - timedelta(seconds=60),
        }
        with (
            patch("teatree.loop.statusline._live_loop_names", return_value=leases),
            patch("teatree.loop.statusline._loop_acquired_ats", return_value=acquired_ats),
            patch("teatree.loop.statusline._cadence_for_loop", return_value=720),
        ):
            lines = live_loops_anchor()
        assert len(lines) == 2, repr(lines)
        joined = "\n".join(lines)
        assert "loop-tick" in joined
        assert "loop-owner" in joined
        for line in lines:
            assert "next " in line, line

    def test_falls_back_to_last_tick_never_when_no_lease_history(self) -> None:
        leases = [("loop-tick", "sessA")]
        with (
            patch("teatree.loop.statusline._live_loop_names", return_value=leases),
            patch("teatree.loop.statusline._loop_acquired_ats", return_value={}),
            patch("teatree.loop.statusline._cadence_for_loop", return_value=720),
        ):
            lines = live_loops_anchor()
        assert lines == ["loop-tick · last tick: never"], repr(lines)

    def test_reports_due_when_overdue(self) -> None:
        leases = [("loop-tick", "sessA")]
        acquired_at = datetime.now(UTC) - timedelta(hours=1)
        with (
            patch("teatree.loop.statusline._live_loop_names", return_value=leases),
            patch(
                "teatree.loop.statusline._loop_acquired_ats",
                return_value={"loop-tick": acquired_at},
            ),
            patch("teatree.loop.statusline._cadence_for_loop", return_value=720),
        ):
            lines = live_loops_anchor()
        assert lines == ["loop-tick · next: due"], repr(lines)

    def test_empty_when_no_loops_live(self) -> None:
        with patch("teatree.loop.statusline._live_loop_names", return_value=[]):
            assert live_loops_anchor() == []

    def test_fails_open_on_db_error(self) -> None:
        with patch("teatree.loop.statusline._live_loop_names", side_effect=RuntimeError("db down")):
            assert live_loops_anchor() == []


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
        with patch("teatree.loop.statusline._live_loop_names", return_value=[]):
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
        with patch("teatree.loop.statusline._live_loop_names", return_value=[]):
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
        with patch("teatree.loop.statusline._live_loop_names", return_value=[]):
            zones = zones_for(actions, colorize=False)
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        assert "(+5 more)" in body, body


class TestZonesForIntegration:
    """End-to-end: ``zones_for`` + ``render`` produces one anchor line per loop."""

    def test_loop_lines_lead_anchors_zone(self, tmp_path: Path) -> None:
        leases = [("loop-tick", "sessA"), ("loop-owner", "sessA")]
        now = datetime.now(UTC)
        acquired_ats = {
            "loop-tick": now - timedelta(seconds=60),
            "loop-owner": now - timedelta(seconds=30),
        }
        with (
            patch("teatree.loop.statusline._live_loop_names", return_value=leases),
            patch("teatree.loop.statusline._loop_acquired_ats", return_value=acquired_ats),
            patch("teatree.loop.statusline._cadence_for_loop", return_value=720),
        ):
            zones = zones_for([], colorize=False)
        target = tmp_path / "statusline.txt"
        render(zones, target=target, colorize=False)
        body = target.read_text()
        assert "loop-tick" in body
        assert "loop-owner" in body
        # ``loop:tick`` / ``loop:owner`` colon-prefixed dump form gone.
        assert "\nloop:tick" not in body
        assert "\nloop:owner" not in body
