"""Is this process still EXECUTING a task claim? — the ORM-free decision layer.

A lapsed lease is evidence about the LEASE, not about the work (#4164). Under memory
pressure the headless runner's event loop stalls past its 900s lease while the agent is
still producing; every sweep that reads a lapsed lease as death then reclaims, fails, or
re-enqueues the row — and a SECOND agent starts on the same worktree while the first is
still executing, so one memory blip costs a run *plus* a full re-execution.

The evidence is the :func:`driving` registry, not a pid probe. ``t3 worker`` drains the
``default`` and ``loops`` queues as THREADS of one process, so the sweeps and the run they
would reap share an address space: a live in-memory entry is a fact about the work, where
an alive pid is only a fact about the process — and in a single-worker deployment the pid
is trivially alive whether or not the job it claimed still exists. Inferring from the pid
regressed recovery instead, holding a crashed job's row for as long as the inference did.

The registry answers only for THIS process; an owner elsewhere gets today's lease-only
verdict, since nothing here can see whether that process is still driving. So the guard can
only ever WITHHOLD a reap it can prove premature, never widen one.
"""

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from teatree.core.loop_lease_liveness import namespace_is_attributable, reader_pid_namespace

#: Task pks this process is executing right now, guarded because the sweeps run on the
#: ``loops`` executor thread while the drive runs on ``default``.
_driving: set[int] = set()
_driving_lock = threading.Lock()

#: The two columns :class:`ClaimOwner` reads, so a ``.values_list()`` caller cannot silently
#: omit one and hand the predicate a blank field that reads as an absent fact.
OWNER_COLUMNS = ("owner_pid", "owner_pid_namespace")


@dataclass(frozen=True, slots=True)
class ClaimOwner:
    """Which process holds a claim: its pid, and the namespace that pid means anything in.

    They travel together because a bare pid is meaningless outside the namespace it was
    recorded in (#4253) — each service in the deployment has its own, so the same integer
    names a different process, or none, depending on who reads it.
    """

    owner_pid: int | None = None
    owner_pid_namespace: str = ""

    @classmethod
    def of(cls, holder: object) -> "ClaimOwner":
        """Build a claim owner from any row carrying :data:`OWNER_COLUMNS` (a ``Task``)."""
        return cls(
            owner_pid=getattr(holder, "owner_pid", None),
            owner_pid_namespace=getattr(holder, "owner_pid_namespace", "") or "",
        )

    def is_this_process(self) -> bool:
        return self.owner_pid == os.getpid() and namespace_is_attributable(self.owner_pid_namespace)


def current_owner() -> tuple[int, str]:
    """This process's ``(pid, pid namespace)`` — the identity every claim write records."""
    return os.getpid(), reader_pid_namespace()


@contextmanager
def driving(task_pk: int) -> Iterator[None]:
    """Mark *task_pk* as executing in this process for the duration of the block.

    Entered around the whole in-process execution of a task, so a sweep on a sibling
    executor thread can tell a stalled run from a dead one. It exits on every path, so a
    crash releases the row to the ordinary lease-expiry recovery rather than pinning it.
    """
    with _driving_lock:
        _driving.add(task_pk)
    try:
        yield
    finally:
        with _driving_lock:
            _driving.discard(task_pk)


def reset_driving_registry() -> None:
    """Empty the registry — test-only, wired into the conftest autouse roster.

    Entries are pk-keyed and sqlite ``TestCase`` rollback recycles rowids, so one leaked
    entry makes a later test's fresh task read as executing and silently withholds a reap
    that test asserts is taken.
    """
    with _driving_lock:
        _driving.clear()


def owner_is_executing(owner: ClaimOwner, task_pk: int) -> bool:
    """Whether THIS process is still executing the claim *owner* describes.

    ``True`` only when the recorded owner is this process AND this process is inside
    :func:`driving` for that task — a fact, not an inference. Everything else is ``False``:
    an owner elsewhere, an unattributable namespace, an unrecorded pid, or a task nothing
    here is driving. That leaves the caller on today's lease-only verdict rather than
    holding a row it cannot prove is alive.
    """
    if not owner.is_this_process():
        return False
    with _driving_lock:
        return task_pk in _driving


def executing_owner_reason(owner: ClaimOwner) -> str:
    """Why a sweep withheld its reap — worded identically by every consumer."""
    return f"pid {owner.owner_pid} is still executing it (the lease lapsed, the run did not)"
