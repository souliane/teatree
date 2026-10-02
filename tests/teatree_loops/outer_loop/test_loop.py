"""The ``outer_loop`` MiniLoop registration — off the live tick (T4-PR-3)."""

from teatree.loops.outer_loop.loop import MINI_LOOP, OUTER_LOOP_NAME
from teatree.loops.registry import iter_loops


class TestMiniLoop:
    def test_is_off_live_tick_with_no_scanner_jobs(self) -> None:
        assert MINI_LOOP.name == OUTER_LOOP_NAME == "outer_loop"
        assert MINI_LOOP.off_live_tick is True
        assert MINI_LOOP.build_jobs() == []

    def test_discovered_by_the_registry(self) -> None:
        assert "outer_loop" in {loop.name for loop in iter_loops()}
