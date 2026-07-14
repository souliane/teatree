"""Registry — discover MINI_LOOP constants via pkgutil.iter_modules."""

import importlib
import logging
import sys

import teatree.loops as _loops_pkg
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


class TestHelperPackageDiscovery:
    """Discovery separates a no-``loop`` helper package from a broken loop.

    A subpackage that carries no ``loop`` submodule (expected, silent) must not
    be conflated with a ``loop`` module that exists but fails to import (a real
    error worth a WARNING).
    """

    def test_shared_helper_package_emits_no_warning(self, caplog) -> None:
        # ``teatree.loops.shared`` is a real utilities subpackage with no
        # ``loop`` submodule; discovery must skip it silently, not WARN on
        # every tick (regression for the per-tick "Skipping loop 'shared'" spam).
        with caplog.at_level(logging.WARNING, logger="teatree.loops.registry"):
            iter_loops()
        offenders = [r.getMessage() for r in caplog.records if "shared" in r.getMessage()]
        assert offenders == [], offenders

    def test_broken_loop_module_still_warns(self, caplog, tmp_path, monkeypatch) -> None:
        # A subpackage whose ``loop`` module EXISTS but imports a missing
        # dependency is a genuine error — it must still surface a WARNING and
        # not be silenced along with the no-loop-submodule case.
        pkg = "brokenloopfixture"
        pkg_dir = tmp_path / pkg
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "loop.py").write_text("import teatree_missing_dependency_xyz\n")
        monkeypatch.setattr(_loops_pkg, "__path__", [*_loops_pkg.__path__, str(tmp_path)])
        importlib.invalidate_caches()
        try:
            with caplog.at_level(logging.WARNING, logger="teatree.loops.registry"):
                iter_loops()
            warned = [
                r.getMessage() for r in caplog.records if pkg in r.getMessage() and "import failed" in r.getMessage()
            ]
            assert warned, [r.getMessage() for r in caplog.records]
        finally:
            sys.modules.pop(f"teatree.loops.{pkg}", None)
            sys.modules.pop(f"teatree.loops.{pkg}.loop", None)
