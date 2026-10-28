"""Resource-safe defaults for direct local pytest calls.

CI's deliberately sharded lanes provide their own explicit xdist budget. A local
``uv run pytest <paths>`` uses this small memory-aware pool, while an unsharded
whole-tree or oversized-directory call is refused by pytest_selection before
collection. Every run leases its xdist workers from one host-wide file-lock pool,
so concurrent runs share the cores instead of each taking them all.
"""

import errno
import fcntl
import os
import sys
import time
from pathlib import Path
from types import TracebackType
from typing import Self

from teatree.utils.hook_registry import loop_registry_dir

TOTAL_TEST_WORKERS_PER_CORE = 1
INHERITED_LEASE_ENV = "TEATREE_PYTEST_WORKER_LEASE"
POOL_ROOT_ENV = "TEATREE_PYTEST_WORKER_POOL_ROOT"

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


class PoolUnavailableError(RuntimeError):
    def __init__(self, reason: str, path: Path) -> None:
        super().__init__(f"shared pytest worker pool {reason}: {path}")


class SlotLease:
    def __init__(self, descriptors: list[int]) -> None:
        self._descriptors = descriptors
        self._inherited_marker: str | None = None

    @property
    def workers(self) -> int:
        # An empty lease is a nested run riding the slot its ancestor's blocked worker holds.
        return max(1, len(self._descriptors))

    def release(self) -> None:
        for descriptor in self._descriptors:
            os.close(descriptor)
        self._descriptors.clear()
        if self._inherited_marker and os.environ.get(INHERITED_LEASE_ENV) == self._inherited_marker:
            os.environ.pop(INHERITED_LEASE_ENV, None)

    def mark_inherited(self, marker: str) -> None:
        self._inherited_marker = marker

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
        if self._inherits_live_lease():
            return SlotLease([])
        lease = self.acquire(requested)
        marker = f"{self.path}:{os.getpid()}"
        os.environ[INHERITED_LEASE_ENV] = marker
        lease.mark_inherited(marker)
        return lease

    def _inherits_live_lease(self) -> bool:
        marker = os.environ.get(INHERITED_LEASE_ENV, "")
        path, separator, pid = marker.rpartition(":")
        if not separator or path != str(self.path) or not pid.isdecimal():
            return False
        for slot in self.path.glob("slot-*.lock"):
            try:
                descriptor = os.open(slot, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
                try:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        if os.read(descriptor, 32).decode().strip() == pid:
                            return True
                finally:
                    os.close(descriptor)
            except OSError:
                continue
        return False

    def acquire(self, requested: int, *, poll_interval: float = 1.0) -> SlotLease:
        try:
            self.path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            reason = "is unavailable"
            raise PoolUnavailableError(reason, self.path) from exc
        reported = False
        while True:
            total = self._shared_capacity()
            target = max(1, min(requested, total))
            descriptors, holder = self._lock_free_slots(target, total)
            if descriptors:
                break
            if not reported:
                sys.stderr.write(
                    f"pytest worker pool at {self.path} has no free slots "
                    f"(capacity {total}); holder pid {holder or 'unknown'}; waiting\n"
                )
                sys.stderr.flush()
                reported = True
            time.sleep(poll_interval)
        if len(descriptors) < requested:
            sys.stderr.write(f"pytest worker pool at {self.path}: granted {len(descriptors)} of {requested}\n")
        return SlotLease(descriptors)

    def _shared_capacity(self) -> int:
        path = self.path / "capacity.lock"
        try:
            root_descriptor = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                fcntl.flock(root_descriptor, fcntl.LOCK_EX)
                try:
                    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EPERM, errno.ELOOP}:
                        raise
                    path.unlink(missing_ok=True)
                    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                    try:
                        recorded = int(os.read(descriptor, 32).decode().strip())
                    except (OSError, UnicodeError, ValueError):
                        recorded = None
                    total = max(self.capacity, recorded or 0)
                    if recorded != total:
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        encoded = f"{total}\n".encode()
                        os.write(descriptor, encoded)
                        os.ftruncate(descriptor, len(encoded))
                    self.capacity = total
                    return total
                finally:
                    os.close(descriptor)
            finally:
                os.close(root_descriptor)
        except OSError as exc:
            reason = "is unavailable"
            raise PoolUnavailableError(reason, self.path) from exc

    def _lock_free_slots(self, target: int, total: int) -> tuple[list[int], str]:
        descriptors: list[int] = []
        holder = ""
        slot_path = self.path
        try:
            for slot in range(total):
                slot_path = self.path / f"slot-{slot:04d}.lock"
                descriptor = os.open(slot_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
                try:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        if not holder:
                            holder = os.read(descriptor, 32).decode().strip()
                        continue
                    os.ftruncate(descriptor, 0)
                    os.write(descriptor, str(os.getpid()).encode())
                    descriptors.append(descriptor)
                finally:
                    if descriptor not in descriptors:
                        os.close(descriptor)
                if len(descriptors) == target:
                    break
        except OSError as exc:
            SlotLease(descriptors).release()
            reason = "is unreadable"
            raise PoolUnavailableError(reason, slot_path) from exc
        return descriptors, holder


def worker_pool_root() -> Path:
    return Path(os.environ.get(POOL_ROOT_ENV) or loop_registry_dir())


__all__ = [
    "INHERITED_LEASE_ENV",
    "POOL_ROOT_ENV",
    "TOTAL_TEST_WORKERS_PER_CORE",
    "PoolUnavailableError",
    "SlotLease",
    "WorkerSlotPool",
    "bounded_auto_workers",
    "worker_pool_root",
]
