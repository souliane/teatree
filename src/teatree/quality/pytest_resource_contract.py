"""Resource-safe defaults for direct local pytest calls.

CI's deliberately sharded lanes provide their own explicit xdist budget. A local
``uv run pytest <paths>`` uses this small memory-aware pool, while an unsharded
whole-tree call is refused before collection can burn the machine.
"""

from pathlib import Path

_LOCAL_MAX_WORKERS = 4
_PARENT_RESERVE_MIB = 512
_WORKER_MIB = 768


def bounded_auto_workers(*, cores: int, memory_mib: int | None, explicit: str | None) -> int:
    """Resolve xdist's ``-n auto`` without ever deriving a local pool from host CPUs alone."""
    try:
        requested = int(explicit or "")
    except ValueError:
        requested = 0
    if requested > 0:
        # The sharded CI lane has already computed this from its cgroup and logs it.
        return requested
    memory_workers = max(1, (memory_mib - _PARENT_RESERVE_MIB) // _WORKER_MIB) if memory_mib else 1
    return max(1, min(_LOCAL_MAX_WORKERS, max(1, cores), memory_workers))


def whole_tree_refusal(args: list[str], *, root: Path, sharded: bool, tach_scoped: bool = False) -> str:
    """Explain an unsharded whole-tree selection before pytest begins collection.

    ``tach_scoped`` deselects at COLLECTION time, so a SCOPED run's explicit
    ``tests`` root — added alongside ``--doctest-modules`` targets so positionals
    don't clobber ``testpaths`` (``affected_tests.py::pytest_args``) — is bounded,
    not a whole-tree request. ``pytest.Config.args`` holds only the leftover
    positionals, never ``--tach`` itself, so the caller reads that flag off
    ``config.getoption`` and passes it in rather than it being inferred here (#4856).
    """
    if sharded or tach_scoped:
        return ""
    tests_root = (root / "tests").resolve()
    selected = [Path(arg) for arg in args if not arg.startswith("-")]
    whole_roots = {root.resolve(), tests_root}
    if not selected or any((path if path.is_absolute() else root / path).resolve() in whole_roots for path in selected):
        return (
            "unsharded whole-tree pytest is refused locally; run dev/ci-parity-fast.sh for scoped feedback "
            "or use the sharded CI lane. For direct test paths, -n auto is bounded by "
            "PYTEST_XDIST_AUTO_NUM_WORKERS and the memory-aware local default."
        )
    return ""


__all__ = ["bounded_auto_workers", "whole_tree_refusal"]
