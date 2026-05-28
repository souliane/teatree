"""Registry — discover MINI_LOOP constants via pkgutil.iter_modules."""

from teatree.loops.base import MiniLoop
from teatree.loops.registry import iter_loops


class TestIterLoops:
    def test_returns_tuple_of_mini_loops(self) -> None:
        loops = iter_loops()
        assert isinstance(loops, tuple)
        assert len(loops) >= 1
        names = {loop.name for loop in loops}
        # Sanity — the always-on dispatch loop must be discovered.
        assert "dispatch" in names

    def test_alphabetical_order(self) -> None:
        loops = iter_loops()
        names = [loop.name for loop in loops]
        assert names == sorted(names)

    def test_excludes_helper_modules(self) -> None:
        # base, registry, orchestrator, cadence_ledger, config, summary
        # are helper modules — they must not be discovered as mini-loops.
        loops = iter_loops()
        names = {loop.name for loop in loops}
        for excluded in ("base", "registry", "orchestrator", "cadence_ledger", "config", "summary"):
            assert excluded not in names

    def test_each_entry_is_a_mini_loop(self) -> None:
        for loop in iter_loops():
            assert isinstance(loop, MiniLoop)
