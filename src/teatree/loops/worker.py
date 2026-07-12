"""The long-lived ``t3 worker`` — the singleton executor pool for the timer chains (#1796).

One process runs programmatic ``django_tasks_db`` :class:`Worker` executor threads —
2 pinned to the ``loops`` queue and a host-scaled ``default`` pool (floored at 2,
:func:`default_queue_executor_count`) — so a heavy headless ``default`` job can never
starve a reactive loop timer, and a deep backlog of independent headless work still
drains in parallel on a bigger box instead of one-or-two-at-a-time. A
supervisor thread re-reads the ``loop_runner_enabled`` kill-switch every ~5 s and
stops every executor on a flip-off or a SIGTERM/SIGINT, joining and — after the join
timeout — SIGKILLing any in-flight tick process group the join left orphaned, then
exiting; the flock singleton (:func:`teatree.utils.singleton.singleton`) guarantees
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

if TYPE_CHECKING:
    from django_tasks_db.management.commands.db_worker import Worker

logger = logging.getLogger(__name__)

#: Reactive-timer executors pinned to the ``loops`` queue — fixed at 2 so a heavy
#: headless ``default`` job can never starve a loop timer.
LOOPS_EXECUTOR_COUNT = 2
#: The prior hardcoded ``default``-queue width; now the FLOOR so a small box keeps
#: the old minimum while a bigger box scales up.
DEFAULT_QUEUE_FLOOR = 2


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
    """The executor pool: 2 ``loops`` threads + a host-scaled ``default`` pool."""
    return ("loops",) * LOOPS_EXECUTOR_COUNT + ("default",) * default_queue_executor_count()


#: The supervisor re-reads the kill-switch on this cadence — a flip-off stops
#: further dispatch within ~this many seconds.
SUPERVISOR_POLL_SECONDS = 5.0
#: Each executor's empty-poll interval — small so a requested stop flips fast.
EXECUTOR_INTERVAL_SECONDS = 1.0


class _Executor(Protocol):
    running: bool

    def run(self) -> None: ...


class _Handle(Protocol):
    def join(self, timeout: float | None = None) -> None: ...


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


def _spawn_executor_thread(executor: _Executor) -> _Handle:
    """Run *executor* in a daemon thread that closes its DB connection on exit."""

    def _run() -> None:
        try:
            executor.run()
        finally:
            from django.db import connections  # noqa: PLC0415 — deferred: Django import at call time

            connections.close_all()

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
    sleep: Callable[[float], None] = time.sleep
    poll_seconds: float = SUPERVISOR_POLL_SECONDS
    executor_queues: tuple[str, ...] = field(default_factory=build_executor_queues)


class LoopWorker:
    """Supervised executor pool: reconcile, drain K queues, stop on kill-switch/signal."""

    def __init__(self, seams: WorkerSeams | None = None) -> None:
        self._seams = seams or WorkerSeams()
        self._stop = threading.Event()
        self._executors: list[_Executor] = []

    def request_stop(self) -> None:
        """Signal the supervisor to shut down (the SIGTERM/SIGINT handler target)."""
        self._stop.set()

    def run(self) -> None:
        """Reconcile, expire stale jobs, start the executors, supervise until stop, then join and exit."""
        seams = self._seams
        seams.reconcile()
        seams.seed_chains()
        # Expire the stale `default`-queue backlog BEFORE any executor spawns, so a box
        # that queued days-old provision/ship jobs while no worker ran never blind-fires
        # them the instant the worker starts (the default-ON flip's load-jam class).
        seams.expire()

        self._executors = [
            seams.make_executor(queue, f"worker-{os.getpid()}-{index}-{queue}")
            for index, queue in enumerate(seams.executor_queues)
        ]
        handles = [seams.spawn(executor) for executor in self._executors]

        try:
            while not self._stop.is_set() and seams.enabled():
                seams.sleep(seams.poll_seconds)
        finally:
            self.request_stop()
            for executor in self._executors:
                executor.running = False
            for handle in handles:
                handle.join(timeout=EXECUTOR_INTERVAL_SECONDS * 3)
            # The daemon-join above never reaches a tick SUBPROCESS: a kill-switch flip
            # or SIGTERM mid-tick orphans it with no deadline owner. Kill any in-flight
            # tick process group so no zombie/orphan outlives the worker's shutdown.
            seams.kill_ticks()
