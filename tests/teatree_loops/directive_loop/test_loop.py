"""The ``directive_loop`` MiniLoop registration — off the live tick (north-star PR-7)."""

from teatree.loops.directive_loop.loop import DIRECTIVE_LOOP_NAME, MINI_LOOP
from teatree.loops.registry import iter_loops
from teatree.loops.seed import DEFAULT_LOOPS


class TestMiniLoop:
    def test_is_off_live_tick_with_no_scanner_jobs(self) -> None:
        assert MINI_LOOP.name == DIRECTIVE_LOOP_NAME == "directive_loop"
        assert MINI_LOOP.off_live_tick is True
        assert MINI_LOOP.build_jobs() == []

    def test_discovered_by_the_registry(self) -> None:
        assert "directive_loop" in {loop.name for loop in iter_loops()}

    def test_is_seeded_disabled_row(self) -> None:
        # Seed/registry parity: the MiniLoop has a matching DEFAULT_LOOPS seed spec
        # (seeded paused, per the #2513 cutover — QUADRUPLE-OFF layer 2).
        assert "directive_loop" in {spec.name for spec in DEFAULT_LOOPS}
