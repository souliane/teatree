"""Is this process still EXECUTING a task claim? — the ORM-free decision layer.

A lapsed lease is evidence about the LEASE, not about the work (#4164). Under memory
pressure the headless runner's event loop stalls past its 900s lease while the agent is
still producing; every sweep that reads a lapsed lease as death then reclaims, fails, or
re-enqueues the row — and a SECOND agent starts on the same worktree while the first is
still executing, so one memory blip costs a run *plus* a full re-execution.

Two tiers of evidence, tried in order. The first is the :func:`driving` registry — a
live in-memory entry is a fact about the work, not an inference — but it answers ONLY for
THIS process: ``reclaim_orphaned_claims`` / ``reap_stale_claims`` run inside the per-loop
``loops_tick`` SUBPROCESS every tick spawns (:mod:`teatree.loops.deadlined_tick`), a
separate interpreter from the ``t3 worker`` process that actually drives headless work, so
the registry there is always empty. The second tier is the cross-process twin: the
persisted ``owner_driving_since`` timestamp (written once at drive-entry, cleared once at
drive-exit by :func:`~teatree.core.models.task_claim.drive_claim` — never renewed, so a
starved event loop that cannot heartbeat still recorded it before the stall began) trusted
only while its ``owner_pid`` is independently proven alive, since a bare pid alone is no
evidence at all (in a single-worker deployment it is trivially alive whether or not the
job it claimed still exists — a first cut inferred liveness from pid alone and held a
crashed job's row for as long as the inference lasted).

Both tiers answer only for the SAME process that recorded the evidence (in-process:
this interpreter; cross-process: the owner_pid's own process); an owner this cannot
resolve gets today's lease-only verdict. So the guard can only ever WITHHOLD a reap it
can prove premature, never widen one.
"""

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from teatree.core.loop_lease_liveness import namespace_is_attributable, pid_alive_probe, reader_pid_namespace

#: Task pks this process is executing right now, guarded because the sweeps run on the
#: ``loops`` executor thread while the drive runs on ``default``.
_driving: set[int] = set()
_driving_lock = threading.Lock()

#: The three columns :class:`ClaimOwner` reads, so a ``.values_list()`` caller cannot
#: silently omit one and hand the predicate a blank field that reads as an absent fact.
OWNER_COLUMNS = ("owner_pid", "owner_pid_namespace", "owner_driving_since")


@dataclass(frozen=True, slots=True)
class ClaimOwner:
    """Which process holds a claim, and whether it is still driving it.

    ``owner_pid``/``owner_pid_namespace`` travel together because a bare pid is
    meaningless outside the namespace it was recorded in (#4253) — each service in
    the deployment has its own, so the same integer names a different process, or
    none, depending on who reads it. ``owner_driving_since`` is the cross-process
    twin of the in-memory :func:`driving` registry (#4164 follow-up) — set once at
    drive-entry, cleared once at drive-exit, so a reader in a DIFFERENT process
    (the ``loops_tick`` subprocess) has evidence too, not just this one's own
    memory.
    """

    owner_pid: int | None = None
    owner_pid_namespace: str = ""
    owner_driving_since: datetime | None = None

    @classmethod
    def of(cls, holder: object) -> "ClaimOwner":
        """Build a claim owner from any row carrying :data:`OWNER_COLUMNS` (a ``Task``)."""
        return cls(
            owner_pid=getattr(holder, "owner_pid", None),
            owner_pid_namespace=getattr(holder, "owner_pid_namespace", "") or "",
            owner_driving_since=getattr(holder, "owner_driving_since", None),
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
    """Whether the owner *owner* describes is still executing the claim on *task_pk*.

    Two tiers, tried in order. Tier one, SAME process: the recorded owner IS this
    process (pid + attributable namespace) AND this process is inside :func:`driving`
    for that task — a fact, read from this interpreter's own memory, not an inference.

    Tier two, A DIFFERENT process (#4164 follow-up): the recorded owner's namespace is
    attributable but the pid is NOT this process's own (the ``loops_tick`` subprocess
    case, where tier one is always empty). Trusts ``owner.owner_driving_since`` only
    while ``owner.owner_pid`` is independently proven alive via
    :func:`~teatree.core.loop_lease_liveness.pid_alive_probe` — a bare pid alone is no
    evidence (trivially alive in a single-worker deployment whether or not the job it
    claimed still exists), and a bare timestamp alone cannot self-clear on a hard crash.

    Everything else is ``False``: an unattributable namespace, no recorded pid, no
    ``owner_driving_since``, or a provably dead pid. That leaves the caller on
    today's lease-only verdict rather than holding a row it cannot prove is alive.
    """
    if owner.is_this_process():
        with _driving_lock:
            return task_pk in _driving
    if owner.owner_pid is None or not namespace_is_attributable(owner.owner_pid_namespace):
        return False
    if owner.owner_driving_since is None:
        return False
    probe = pid_alive_probe()
    return probe is not None and probe(owner.owner_pid)


def executing_owner_reason(owner: ClaimOwner) -> str:
    """Why a sweep withheld its reap — worded identically by every consumer."""
    return f"pid {owner.owner_pid} is still executing it (the lease lapsed, the run did not)"
