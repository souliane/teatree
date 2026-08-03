"""The long-lived ``t3 worker`` — the singleton executor pool for the timer chains (#1796).

One process runs programmatic ``django_tasks_db`` :class:`Worker` executor threads —
a host-scaled ``loops`` pool (floored at 2, :func:`loops_executor_count`) and a
host-scaled ``default`` pool (floored at 2, :func:`default_queue_executor_count`) —
so a heavy headless ``default`` job can never starve a reactive loop timer, two slow
loop ticks can never stall every OTHER loop's timer, and a deep backlog of independent
headless work still drains in parallel on a bigger box instead of one-or-two-at-a-time.
A supervisor thread re-reads the ``loop_runner_enabled`` kill-switch every ~5 s AND
polls each executor thread's :meth:`is_alive`, respawning any that a swallowed error
(a ``DBTaskResult`` ``OperationalError`` inside ``db_worker``) silently killed — so a
dead executor never freezes the whole box while the process still looks healthy. It
stops every executor on a flip-off or a SIGTERM/SIGINT, joining and — after the join
timeout — SIGKILLing any in-flight tick process group the join left orphaned, then
exiting; when a single executor exhausts its respawn budget the worker exits NON-ZERO
(loud, never silent) so the OS/container restarts it fresh rather than limping with a
dead pool. The flock singleton (:func:`teatree.utils.singleton.singleton`) guarantees
at most one worker per box. At startup the worker reconciles the loop-timer chains, seeds
the maintenance chains — including the ``drive_off_live_tick_loops`` chain that fires
the tick command of every ``off_live_tick`` loop, the ONLY driver those loops have — and
expires the stale ``default``-queue backlog BEFORE spawning executors (so a box that
queued days-old provision/ship jobs while no worker ran never blind-fires them on the
default-ON flip), so a fresh or crash-recovered box catches up and
self-heals with no OS scheduler (no cron / launchd / systemd). The worker supervisor +
reconciler IS the process-watchdog surface.
"""

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from teatree.loop.queue_drain import expire_stale_default_jobs
from teatree.loops.deadlined_tick import kill_live_tick_process_groups
from teatree.loops.timer_chains import LoopRunnerState, read_loop_runner_state
from teatree.loops.timer_reconciler import ensure_loop_timers, ensure_maintenance_chains
from teatree.utils.ram_probe import default_provision_concurrency
from teatree.utils.thread_db import close_thread_db_connections

if TYPE_CHECKING:
    from django_tasks_db.management.commands.db_worker import Worker

logger = logging.getLogger(__name__)

#: The prior hardcoded ``loops``-queue width; now the FLOOR so a small box keeps the
#: old minimum (2 reactive-timer threads) while a bigger box scales up — two slow loop
#: ticks pinning both floor threads no longer stalls every OTHER loop's timer.
LOOPS_EXECUTOR_FLOOR = 2
#: The prior hardcoded ``default``-queue width; now the FLOOR so a small box keeps
#: the old minimum while a bigger box scales up.
DEFAULT_QUEUE_FLOOR = 2


def loops_executor_count() -> int:
    """Host-scaled width of the ``loops`` (reactive-timer) executor pool, floored at 2.

    A fixed 2 threads serialise every loop timer fire behind at most two in-flight
    ticks, so two slow ticks stall every other loop's timer plus the maintenance /
    reconcile chains. Scaling with the shared PR-01 resource ceiling
    (:func:`default_provision_concurrency` — half the logical cores) lets a bigger
    box fire more timers in parallel; the floor preserves the prior minimum on a
    1-2 core box.
    """
    return max(LOOPS_EXECUTOR_FLOOR, default_provision_concurrency())


def default_queue_executor_count() -> int:
    """Host-scaled width of the ``default`` (FSM/headless) executor pool, floored at 2.

    A deep backlog of independent PRs drained through a fixed 2 threads reviews and
    merges one-or-two-at-a-time regardless of host size. Scaling with the shared
    PR-01 resource ceiling (:func:`default_provision_concurrency` — half the logical
    cores) lets an idle multi-core box run more phase work in parallel; the floor
    preserves the prior minimum on a 1-2 core box.
    """
    return max(DEFAULT_QUEUE_FLOOR, default_provision_concurrency())


def build_executor_queues() -> tuple[str, ...]:
    """The executor pool: a host-scaled ``loops`` pool + a host-scaled ``default`` pool."""
    return ("loops",) * loops_executor_count() + ("default",) * default_queue_executor_count()


#: The supervisor re-reads the kill-switch on this cadence — a flip-off stops
#: further dispatch within ~this many seconds.
SUPERVISOR_POLL_SECONDS = 5.0
#: How often the supervisor re-claims ``t3-master`` (#3968). Well inside the 1800 s
#: lease TTL, and far rarer than the 5 s kill-switch poll so the heartbeat adds no
#: meaningful write pressure to the control DB. The re-claim also self-heals the slot
#: once a ``t3 loop claim --take-over`` by a since-dead session goes stale — which is
#: the "a transient session's claim rots" shape the ticket warns against.
T3_MASTER_REFRESH_SECONDS = 300.0
#: Each executor's empty-poll interval — small so a requested stop flips fast.
EXECUTOR_INTERVAL_SECONDS = 1.0
#: How many times a single executor slot may be respawned within one worker
#: lifetime before the worker gives up and exits NON-ZERO (a crash-looping executor
#: is a real fault the OS/container should restart the whole worker for, not one the
#: supervisor should mask by respawning forever).
MAX_EXECUTOR_RESPAWNS = 5
#: How many consecutive supervisor polls the kill-switch may read UNREADABLE before the
#: worker exits NON-ZERO so ``restart: on-failure`` restarts it (F7). A transient blip
#: recovers within a poll or two; a persistent read failure is a real fault, never a
#: clean stop that leaves the factory silently dead.
MAX_UNREADABLE_POLLS = 3


class _Executor(Protocol):
    running: bool

    def run(self) -> None: ...


class _Handle(Protocol):
    def is_alive(self) -> bool: ...

    def join(self, timeout: float | None = None) -> None: ...


class LoopWorkerExecutorCrashError(RuntimeError):
    """A ``loops``/``default`` executor thread died and exhausted its respawn budget.

    Raised out of :meth:`LoopWorker.run` (after the pool is torn down) so the worker
    process exits NON-ZERO: a repeatedly-crashing executor is a genuine fault the
    OS/container must restart the worker for, never one the supervisor silently masks.
    """


class KillSwitchUnreadableError(RuntimeError):
    """The kill-switch read UNREADABLE for too many consecutive polls (F7).

    Raised out of :meth:`LoopWorker.run` so the worker exits NON-ZERO: a persistent
    kill-switch read failure is a real fault the supervisor must restart the worker
    for, never a clean exit-0 that ``restart: on-failure`` ignores while the factory
    sits silently dead. A legitimate OFF is a clean stop; only "cannot confirm" crashes.
    """


_CRASH_MESSAGE = "A loops/default executor thread died and exhausted its respawn budget; exiting non-zero."
_UNREADABLE_MESSAGE = "The loop_runner_enabled kill-switch was unreadable for too many polls; exiting non-zero."


def _build_executor(queue_name: str, worker_id: str) -> "Worker":
    """A programmatic ``db_worker`` executor drained forever on ONE queue."""
    from django_tasks import DEFAULT_TASK_BACKEND_ALIAS  # noqa: PLC0415 — deferred: heavy/optional dep at call site
    from django_tasks_db.management.commands.db_worker import Worker  # noqa: PLC0415 — deferred: heavy/optional dep

    return Worker(
        queue_names=[queue_name],
        interval=EXECUTOR_INTERVAL_SECONDS,
        batch=False,
        backend_name=DEFAULT_TASK_BACKEND_ALIAS,
        startup_delay=False,
        max_tasks=None,
        worker_id=worker_id,
    )


def _reclaim_dead_owner_leases() -> None:
    """Return every ``loop:<name>``/``t3-master`` lease held by a dead session to the pool (#3571).

    The worker supervisor's runtime half of the dead-owner reclaim (``run_boot_sweeps``
    owns the boot half): a loop whose owning session crashed — or whose pid was reused /
    lives in another container namespace — is otherwise SKIPped by the live worker
    forever. Lazy-imported so the module's import graph carries no Django/ORM edge.
    """
    from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry

    LoopLease.objects.reclaim_dead_owner_leases()


def _claim_t3_master() -> None:
    """Claim/refresh the machine-wide ``t3-master`` slot for this worker (#3968).

    Nothing claimed it before, so ``t3 loop owner`` reported "unclaimed" while this
    process drove every registry loop — and the two owner-gated reactive loops
    (``loop_slack_answer`` / ``loop_self_improve``) deferred forever to an owner that
    never existed. The principal is the durable :data:`LOOP_RUNNER_SESSION_ID`
    constant rather than a per-process id so a worker restart is never locked out of
    its own lease for a full TTL (#3810); liveness rides on ``owner_pid`` instead.

    The claim is a CAS that never evicts a live owner: an interactive session holding
    the slot keeps it, and this worker takes it over on a later refresh once that
    lease lapses.
    """
    from teatree.core.loop_lease_manager import T3_MASTER_SLOT  # noqa: PLC0415 — deferred: pulls in django.db
    from teatree.core.models import LoopDriver, LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry
    from teatree.core.session_identity import LOOP_RUNNER_SESSION_ID  # noqa: PLC0415 — deferred: cheap, kept local

    won, owner = LoopLease.objects.claim_ownership(
        T3_MASTER_SLOT,
        session_id=LOOP_RUNNER_SESSION_ID,
        owner_pid=os.getpid(),
        driver=LoopDriver.LOOP_RUNNER,
    )
    if not won:
        logger.warning(
            "t3-master is held by live session %r; this worker drives loop ticks without owning the slot, so the "
            "owner-gated reactive loops defer to that session until its lease lapses (#3968).",
            owner,
        )


def _release_t3_master() -> None:
    """Hand ``t3-master`` back at shutdown — CAS'd, so a session take-over is untouched."""
    from teatree.core.loop_lease_manager import T3_MASTER_SLOT  # noqa: PLC0415 — deferred: pulls in django.db
    from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry
    from teatree.core.session_identity import LOOP_RUNNER_SESSION_ID  # noqa: PLC0415 — deferred: cheap, kept local

    LoopLease.objects.release_ownership(T3_MASTER_SLOT, session_id=LOOP_RUNNER_SESSION_ID)


def _spawn_executor_thread(executor: _Executor) -> _Handle:
    """Run *executor* in a daemon thread that closes its DB connection on exit.

    Closes the raw DB-API handle rather than calling ``connections.close_all()``:
    that is a documented no-op under the in-memory test database, so it left this
    thread's handle stranded for a later GC. See :mod:`teatree.utils.thread_db`.
    """

    def _run() -> None:
        try:
            executor.run()
        finally:
            close_thread_db_connections()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


@dataclass(frozen=True)
class WorkerSeams:
    """The injectable collaborators — the production defaults wire the real seams.

    Grouped so the supervision/lifecycle logic is tested without real threads, a
    real DB, or a real clock, while keeping :class:`LoopWorker`'s constructor thin.
    """

    read_state: Callable[[], LoopRunnerState] = read_loop_runner_state
    reconcile: Callable[[], object] = ensure_loop_timers
    seed_chains: Callable[[], object] = ensure_maintenance_chains
    expire: Callable[[], object] = expire_stale_default_jobs
    make_executor: Callable[[str, str], _Executor] = _build_executor
    spawn: Callable[[_Executor], _Handle] = _spawn_executor_thread
    kill_ticks: Callable[[], object] = kill_live_tick_process_groups
    reclaim_leases: Callable[[], object] = _reclaim_dead_owner_leases
    claim_master: Callable[[], object] = _claim_t3_master
    release_master: Callable[[], object] = _release_t3_master
    sleep: Callable[[float], None] = time.sleep
    poll_seconds: float = SUPERVISOR_POLL_SECONDS
    master_refresh_seconds: float = T3_MASTER_REFRESH_SECONDS
    max_respawns: int = MAX_EXECUTOR_RESPAWNS
    max_unreadable_polls: int = MAX_UNREADABLE_POLLS
    executor_queues: tuple[str, ...] = field(default_factory=build_executor_queues)


@dataclass
class _Slot:
    """One executor thread plus the queue + respawn bookkeeping to resurrect it."""

    queue: str
    index: int
    executor: _Executor
    handle: _Handle
    respawns: int = 0


class LoopWorker:
    """Supervised executor pool: reconcile, drain K queues, respawn dead threads, stop on kill-switch/signal."""

    def __init__(self, seams: WorkerSeams | None = None) -> None:
        self._seams = seams or WorkerSeams()
        self._stop = threading.Event()
        self._slots: list[_Slot] = []
        self._polls_since_master_refresh = 0

    def request_stop(self) -> None:
        """Signal the supervisor to shut down (the SIGTERM/SIGINT handler target)."""
        self._stop.set()

    def _spawn_slot(self, queue: str, index: int, *, respawns: int = 0) -> _Slot:
        executor = self._seams.make_executor(queue, f"worker-{os.getpid()}-{index}-{queue}")
        return _Slot(queue=queue, index=index, executor=executor, handle=self._seams.spawn(executor), respawns=respawns)

    def _respawn_dead_executors(self) -> bool:
        """Respawn any executor thread that died; return True iff one exhausted its respawn budget.

        A ``db_worker`` executor thread that hits a swallowed error (a ``DBTaskResult``
        ``OperationalError``) exits silently — the pinned queue then never drains and
        every timer chain on it freezes machine-wide while the process still looks
        healthy. Polling :meth:`is_alive` and respawning keeps the pool live; a slot
        that keeps dying past :attr:`WorkerSeams.max_respawns` is a real fault, so the
        caller exits the worker NON-ZERO instead of masking it.
        """
        for i, slot in enumerate(self._slots):
            if slot.handle.is_alive():
                continue
            if slot.respawns >= self._seams.max_respawns:
                logger.error(
                    "Executor for queue %r (slot %d) died %d times — giving up; the worker will exit non-zero.",
                    slot.queue,
                    slot.index,
                    slot.respawns,
                )
                return True
            logger.warning(
                "Executor for queue %r (slot %d) died; respawning (respawn %d).",
                slot.queue,
                slot.index,
                slot.respawns + 1,
            )
            self._slots[i] = self._spawn_slot(slot.queue, slot.index, respawns=slot.respawns + 1)
        return False

    def _reclaim_dead_owner_leases(self) -> None:
        """Sweep dead-owner loop leases; a reclaim error must never crash the supervisor (#3571)."""
        try:
            self._seams.reclaim_leases()
        except Exception:
            logger.warning("Dead-owner loop-lease reclaim failed this poll; will retry next tick.", exc_info=True)

    def _claim_t3_master(self) -> None:
        """Claim/refresh t3-master; a claim error must never crash the supervisor (#3968)."""
        try:
            self._seams.claim_master()
        except Exception:
            logger.warning("t3-master claim failed; will retry on the next refresh.", exc_info=True)

    def _release_t3_master(self) -> None:
        """Hand t3-master back; a release error must never mask the shutdown reason (#3968)."""
        try:
            self._seams.release_master()
        except Exception:
            logger.warning("t3-master release failed; the lease will lapse on its TTL.", exc_info=True)

    def _polls_per_master_refresh(self) -> int:
        """Supervisor polls between two t3-master re-claims, floored at one.

        A zero/negative poll interval (the test seam, and a degenerate config) would
        divide by zero, so it degrades to re-claiming every poll — the conservative
        end, never a skipped heartbeat.
        """
        seams = self._seams
        if seams.poll_seconds <= 0:
            return 1
        return max(1, round(seams.master_refresh_seconds / seams.poll_seconds))

    def _per_poll_maintenance(self) -> None:
        """The supervisor's per-poll upkeep: the throttled t3-master heartbeat, then the lease sweep."""
        self._polls_since_master_refresh += 1
        if self._polls_since_master_refresh >= self._polls_per_master_refresh():
            self._polls_since_master_refresh = 0
            self._claim_t3_master()
        self._reclaim_dead_owner_leases()

    def run(self) -> None:
        """Reconcile, expire stale jobs, start the executors, supervise (kill-switch + liveness), then join and exit."""
        seams = self._seams
        # Ownership and driving are ONE startup (#3968): the slot is claimed before the
        # chains that fire ticks exist, so `t3 loop owner` can never report "unclaimed"
        # while this process drives loops.
        self._claim_t3_master()
        seams.reconcile()
        seams.seed_chains()
        # Expire the stale `default`-queue backlog BEFORE any executor spawns, so a box
        # that queued days-old provision/ship jobs while no worker ran never blind-fires
        # them the instant the worker starts (the default-ON flip's load-jam class).
        seams.expire()

        self._slots = [self._spawn_slot(queue, index) for index, queue in enumerate(seams.executor_queues)]

        crashed = False
        unreadable = False
        unreadable_polls = 0
        try:
            while not self._stop.is_set():
                state = seams.read_state()
                if state is LoopRunnerState.OFF:
                    break  # a legitimate kill-switch OFF is a clean stop (exit 0).
                if state is LoopRunnerState.UNREADABLE:
                    # F7: a read FAILURE is not an OFF — never a clean exit. Retry a few
                    # polls (a blip recovers), then crash so restart:on-failure restarts us.
                    unreadable_polls += 1
                    logger.warning(
                        "kill-switch unreadable (%d/%d consecutive polls) — will crash-restart if it persists",
                        unreadable_polls,
                        seams.max_unreadable_polls,
                    )
                    if unreadable_polls >= seams.max_unreadable_polls:
                        unreadable = True
                        break
                else:
                    unreadable_polls = 0  # ON — a recovered read resets the streak.
                seams.sleep(seams.poll_seconds)
                if self._respawn_dead_executors():
                    crashed = True
                    break
                self._per_poll_maintenance()
        finally:
            self.request_stop()
            for slot in self._slots:
                slot.executor.running = False
            for slot in self._slots:
                slot.handle.join(timeout=EXECUTOR_INTERVAL_SECONDS * 3)
            # The daemon-join above never reaches a tick SUBPROCESS: a kill-switch flip
            # or SIGTERM mid-tick orphans it with no deadline owner. Kill any in-flight
            # tick process group so no zombie/orphan outlives the worker's shutdown.
            seams.kill_ticks()
            # Hand t3-master back so a restarting worker (or an operator's session)
            # finds an unowned slot instead of waiting out this process's TTL.
            self._release_t3_master()
        if crashed:
            raise LoopWorkerExecutorCrashError(_CRASH_MESSAGE)
        if unreadable:
            raise KillSwitchUnreadableError(_UNREADABLE_MESSAGE)
