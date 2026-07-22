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
the maintenance chains, and expires the stale ``default``-queue backlog BEFORE spawning
executors (so a box that queued days-old provision/ship jobs while no worker ran never
blind-fires them on the default-ON flip), so a fresh or crash-recovered box catches up and
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
from teatree.loops.timer_chains import _loop_runner_enabled, kill_live_tick_process_groups
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
#: Each executor's empty-poll interval — small so a requested stop flips fast.
EXECUTOR_INTERVAL_SECONDS = 1.0
#: How many times a single executor slot may be respawned within one worker
#: lifetime before the worker gives up and exits NON-ZERO (a crash-looping executor
#: is a real fault the OS/container should restart the whole worker for, not one the
#: supervisor should mask by respawning forever).
MAX_EXECUTOR_RESPAWNS = 5


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


_CRASH_MESSAGE = "A loops/default executor thread died and exhausted its respawn budget; exiting non-zero."


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

    enabled: Callable[[], bool] = _loop_runner_enabled
    reconcile: Callable[[], object] = ensure_loop_timers
    seed_chains: Callable[[], object] = ensure_maintenance_chains
    expire: Callable[[], object] = expire_stale_default_jobs
    make_executor: Callable[[str, str], _Executor] = _build_executor
    spawn: Callable[[_Executor], _Handle] = _spawn_executor_thread
    kill_ticks: Callable[[], object] = kill_live_tick_process_groups
    reclaim_leases: Callable[[], object] = _reclaim_dead_owner_leases
    sleep: Callable[[float], None] = time.sleep
    poll_seconds: float = SUPERVISOR_POLL_SECONDS
    max_respawns: int = MAX_EXECUTOR_RESPAWNS
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

    def run(self) -> None:
        """Reconcile, expire stale jobs, start the executors, supervise (kill-switch + liveness), then join and exit."""
        seams = self._seams
        seams.reconcile()
        seams.seed_chains()
        # Expire the stale `default`-queue backlog BEFORE any executor spawns, so a box
        # that queued days-old provision/ship jobs while no worker ran never blind-fires
        # them the instant the worker starts (the default-ON flip's load-jam class).
        seams.expire()

        self._slots = [self._spawn_slot(queue, index) for index, queue in enumerate(seams.executor_queues)]

        crashed = False
        try:
            while not self._stop.is_set() and seams.enabled():
                seams.sleep(seams.poll_seconds)
                if self._respawn_dead_executors():
                    crashed = True
                    break
                self._reclaim_dead_owner_leases()
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
        if crashed:
            raise LoopWorkerExecutorCrashError(_CRASH_MESSAGE)
