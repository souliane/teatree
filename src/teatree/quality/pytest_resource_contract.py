"""Resource-safe defaults for direct local pytest calls.

CI's deliberately sharded lanes provide their own explicit xdist budget. A local
``uv run pytest <paths>`` uses this small memory-aware pool, while an unsharded
whole-tree call is refused before collection can burn the machine. Every run then
leases its xdist workers from one host-wide file-lock pool, so concurrent runs share
the cores instead of each taking them all.
"""

import fcntl
import os
import sys
import time
from pathlib import Path
from types import TracebackType
from typing import Self

TOTAL_TEST_WORKERS_PER_CORE = 1
INHERITED_LEASE_ENV = "TEATREE_PYTEST_WORKER_LEASE"

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


def whole_tree_refusal(
    args: list[str], *, root: Path, sharded: bool, tach_active: bool = False, full_selected: bool = False
) -> str:
    """Explain an unsharded whole-tree selection before pytest begins collection.

    ``tach_active`` is the impact-analysis plugin's own parsed ``--tach`` option.
    ``SelectionResult.pytest_args`` emits two SCOPED shapes and neither passes an
    explicit test id: a flags-only invocation (no changed src modules — ``config.args``
    is empty) and a ``--doctest-modules`` one that passes the literal ``tests`` root
    (so the positionals do not clobber ``testpaths``) alongside the changed modules.
    Read by positional args alone, both look like the accidental bare whole-tree call
    this guards against; ``--tach`` deselects at collection time regardless of what
    positionals are given, so it is scoped independently of them.
    """
    if sharded or tach_active or full_selected:
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


class PoolUnavailableError(RuntimeError):
    def __init__(self, reason: str, path: Path) -> None:
        super().__init__(f"shared pytest worker pool {reason}: {path}")


class SlotLease:
    def __init__(self, descriptors: list[int]) -> None:
        self._descriptors = descriptors

    @property
    def workers(self) -> int:
        # An empty lease is a nested run riding the slot its ancestor's blocked worker holds.
        return max(1, len(self._descriptors))

    def release(self) -> None:
        for descriptor in self._descriptors:
            os.close(descriptor)
        self._descriptors.clear()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.release()


class WorkerSlotPool:
    def __init__(self, shared_root: Path, *, capacity: int) -> None:
        self.path = shared_root / "pytest-worker-slots"
        self.capacity = max(1, capacity)

    def session_lease(self, requested: int) -> SlotLease:
        if os.environ.get(INHERITED_LEASE_ENV):
            return SlotLease([])
        lease = self.acquire(requested)
        os.environ[INHERITED_LEASE_ENV] = str(self.path)
        return lease

    def acquire(self, requested: int, *, poll_interval: float = 1.0) -> SlotLease:
        if not self.path.parent.is_dir():
            reason = "root is missing"
            raise PoolUnavailableError(reason, self.path.parent)
        try:
            self.path.mkdir(exist_ok=True)
        except OSError as exc:
            reason = "is unavailable"
            raise PoolUnavailableError(reason, self.path) from exc
        target = max(1, min(requested, self.capacity))
        reported = False
        while not (descriptors := self._lock_free_slots(target)):
            if not reported:
                sys.stderr.write(
                    f"pytest worker pool at {self.path} has no free slots ({self.capacity} total); waiting\n"
                )
                sys.stderr.flush()
                reported = True
            time.sleep(poll_interval)
        if len(descriptors) < requested:
            sys.stderr.write(
                f"pytest worker pool at {self.path}: running {len(descriptors)} of {requested} requested workers\n"
            )
        return SlotLease(descriptors)

    def _lock_free_slots(self, target: int) -> list[int]:
        descriptors: list[int] = []
        try:
            for slot in range(self.capacity):
                descriptor = os.open(
                    self.path / f"slot-{slot:04d}.lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
                )
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(descriptor)
                    continue
                descriptors.append(descriptor)
                if len(descriptors) == target:
                    break
        except OSError as exc:
            SlotLease(descriptors).release()
            reason = "is unreadable"
            raise PoolUnavailableError(reason, self.path) from exc
        return descriptors


__all__ = [
    "INHERITED_LEASE_ENV",
    "TOTAL_TEST_WORKERS_PER_CORE",
    "PoolUnavailableError",
    "SlotLease",
    "WorkerSlotPool",
    "bounded_auto_workers",
    "whole_tree_refusal",
]
