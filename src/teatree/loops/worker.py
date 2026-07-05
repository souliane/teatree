"""The long-lived ``t3 worker`` — the singleton executor pool for the timer chains (#1796).

One process runs K=4 programmatic ``django_tasks_db`` :class:`Worker` executor
threads — 2 pinned to the ``loops`` queue, 2 to ``default`` — so a heavy headless
``default`` job can never starve a reactive loop timer, and vice-versa. A
supervisor thread re-reads the ``loop_runner_enabled`` kill-switch every ~5 s and
stops every executor on a flip-off or a SIGTERM/SIGINT, joining and — after the join
timeout — SIGKILLing any in-flight tick process group the join left orphaned, then
exiting; the flock singleton (:func:`teatree.utils.singleton.singleton`) guarantees
at most one worker per box. At startup the worker reconciles the loop-timer chains and seeds
the maintenance chains, so a fresh or crash-recovered box catches up and self-heals
with no OS scheduler (no cron / launchd / systemd). The worker supervisor +
reconciler IS the process-watchdog surface.
"""

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from teatree.loops.timer_chains import _loop_runner_enabled, kill_live_tick_process_groups
from teatree.loops.timer_reconciler import ensure_loop_timers, ensure_maintenance_chains

if TYPE_CHECKING:
    from django_tasks_db.management.commands.db_worker import Worker

logger = logging.getLogger(__name__)

#: The executor pool: 2 threads pinned to ``loops`` (reactive timers), 2 to
#: ``default`` (FSM/headless work), so neither lane starves the other.
EXECUTOR_QUEUES: tuple[str, ...] = ("loops", "loops", "default", "default")

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
    from django_tasks import DEFAULT_TASK_BACKEND_ALIAS  # noqa: PLC0415
    from django_tasks_db.management.commands.db_worker import Worker  # noqa: PLC0415

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
            from django.db import connections  # noqa: PLC0415

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
    make_executor: Callable[[str, str], _Executor] = _build_executor
    spawn: Callable[[_Executor], _Handle] = _spawn_executor_thread
    kill_ticks: Callable[[], object] = kill_live_tick_process_groups
    sleep: Callable[[float], None] = time.sleep
    poll_seconds: float = SUPERVISOR_POLL_SECONDS
    executor_queues: tuple[str, ...] = EXECUTOR_QUEUES


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
        """Reconcile, start the executors, supervise until stop, then join and exit."""
        seams = self._seams
        seams.reconcile()
        seams.seed_chains()

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
