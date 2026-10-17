"""Lock-tolerant executor threads for the worker's queue slots."""

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from django.db import OperationalError
from django.tasks import DEFAULT_TASK_BACKEND_ALIAS
from django_tasks_db.management.commands.db_worker import Worker

from teatree.core.modelkit.db_retry import is_locked_error
from teatree.utils.thread_db import close_thread_db_connections

logger = logging.getLogger(__name__)

EXECUTOR_INTERVAL_SECONDS = 1.0
LOCK_BACKOFF_FIRST_SECONDS = 0.5
LOCK_BACKOFF_CAP_SECONDS = 8.0
LOCK_QUIET_SECONDS = 60.0
LOCK_ALERT_SECONDS = 600.0
EXECUTOR_HEALTH_WINDOW_SECONDS = 600.0


class Executor(Protocol):
    running: bool

    def run(self) -> None: ...


class Handle(Protocol):
    def is_alive(self) -> bool: ...

    def join(self, timeout: float | None = None) -> None: ...


class CheckpointSlot(Protocol):
    executor: Executor
    handle: Handle


class CheckpointClock(Protocol):
    @property
    def begin_exit(self) -> Callable[[str], None]: ...

    @property
    def monotonic(self) -> Callable[[], float]: ...

    @property
    def sleep(self) -> Callable[[float], None]: ...


def checkpoint_executor_threads(
    slots: Sequence[CheckpointSlot], reason: str, grace_seconds: float, clock: CheckpointClock
) -> None:
    clock.begin_exit(reason)
    for slot in slots:
        slot.executor.running = False
    deadline = clock.monotonic() + grace_seconds
    while any(slot.handle.is_alive() for slot in slots):
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            logger.error("Executor pool still active after checkpoint grace: %s", reason)
            break
        clock.sleep(min(remaining, EXECUTOR_INTERVAL_SECONDS))


def build_executor(queue_name: str, worker_id: str) -> Worker:
    return Worker(
        queue_names=queue_name.split(","),
        interval=EXECUTOR_INTERVAL_SECONDS,
        batch=False,
        backend_name=DEFAULT_TASK_BACKEND_ALIAS,
        startup_delay=False,
        max_tasks=None,
        worker_id=worker_id,
        excluded_queue_names=[],
    )


@dataclass
class LockTolerantRun:
    executor: Executor
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic

    def run(self) -> None:
        delay = LOCK_BACKOFF_FIRST_SECONDS
        streak_started: float | None = None
        alerted = False
        attempts = 0
        while self.executor.running:
            started = self.monotonic()
            try:
                self.executor.run()
            except OperationalError as exc:
                if not is_locked_error(exc):
                    raise
                now = self.monotonic()
                if streak_started is None or now - started >= LOCK_QUIET_SECONDS:
                    streak_started = now
                    delay = LOCK_BACKOFF_FIRST_SECONDS
                    attempts = 0
                    alerted = False
                attempts += 1
                age = now - streak_started
                logger.warning(
                    "Executor database lock attempt %d (%.0fs streak); retrying in %.1fs", attempts, age, delay
                )
                if age >= LOCK_ALERT_SECONDS and not alerted:
                    logger.exception("Executor database lock has persisted for %.0fs; still retrying", age)
                    alerted = True
            else:
                return
            finally:
                close_thread_db_connections()
            self._wait(delay)
            delay = min(delay * 2, LOCK_BACKOFF_CAP_SECONDS)

    def _wait(self, seconds: float) -> None:
        remaining = seconds
        while remaining > 0 and self.executor.running:
            duration = min(remaining, EXECUTOR_INTERVAL_SECONDS)
            self.sleep(duration)
            remaining -= duration


def spawn_executor_thread(
    executor: Executor,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Handle:
    thread = threading.Thread(target=LockTolerantRun(executor, sleep=sleep, monotonic=monotonic).run, daemon=True)
    thread.start()
    return thread
