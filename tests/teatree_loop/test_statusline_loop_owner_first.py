"""The loop line leads with loop chunks, the owner badge rides front, no ``loop running``.

Two coupled contracts:

1.  :func:`live_loops_anchor` no longer emits the leading ``loop running``
    state word. Loop liveness is already carried by the per-loop ``tick
    <next-tick>`` chunk (both derive from the same live
    :class:`~teatree.core.models.LoopLease` set), so the word was redundant
    with the tick token. The line now leads with the first loop chunk.

2.  ``hooks/scripts/statusline.sh`` PREPENDS the per-session ``t3-master:``
    badge to the front of the loop line (and to a stand-alone line when no
    loop line is present), so the user reads ownership first.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from teatree.loop.statusline import live_loops_anchor


class TestLoopRunningTokenDropped:
    """``loop running`` is gone; liveness still shows via the ``tick`` chunk."""

    def test_line_leads_with_tick_chunk_not_loop_running(self) -> None:
        acquired_at = datetime.now(UTC) - timedelta(seconds=120)
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[("loop-tick", acquired_at)]),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch("teatree.loop.statusline_loops._availability_segment", return_value=""),
            patch("teatree.loop.statusline_loops._pending_questions", return_value=0),
        ):
            lines = live_loops_anchor()
        assert len(lines) == 1, repr(lines)
        line = lines[0]
        # The redundant leading state word is gone.
        assert "loop running" not in line, line
        # Liveness is still represented — the tick chunk leads the line.
        assert line.startswith("tick "), line
        assert "tick 10m" in line, line

    def test_mini_loop_only_line_has_no_loop_running(self) -> None:
        now = datetime.now(UTC)
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[]),
            patch(
                "teatree.loop.statusline_loops._mini_loop_schedules",
                return_value=[("dispatch", now + timedelta(seconds=120), 600)],
            ),
            patch("teatree.loop.statusline_loops._availability_segment", return_value=""),
            patch("teatree.loop.statusline_loops._pending_questions", return_value=0),
        ):
            lines = live_loops_anchor()
        assert lines == ["dispatch 2m"], lines

    def test_waiting_clause_still_appended(self) -> None:
        acquired_at = datetime.now(UTC) - timedelta(seconds=120)
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[("loop-tick", acquired_at)]),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch("teatree.loop.statusline_loops._availability_segment", return_value=""),
            patch("teatree.loop.statusline_loops._pending_questions", return_value=2),
        ):
            lines = live_loops_anchor()
        assert lines == ["tick 10m · waiting: 2 questions"], lines

    def test_still_empty_when_no_loops_live(self) -> None:
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[]),
            patch("teatree.loop.statusline_loops._mini_loop_schedules", return_value=[]),
        ):
            assert live_loops_anchor() == []
