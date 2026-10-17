import sqlite3

import pytest
from django.db import OperationalError, connection
from django.test import TestCase

from teatree.loops.executor_thread import LockTolerantRun, build_executor, spawn_executor_thread


class ScriptedExecutor:
    def __init__(self, failures: int, *, error: str = "database is locked") -> None:
        self.running = True
        self.failures = failures
        self.error = error
        self.calls = 0

    def run(self) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise OperationalError(self.error)


def test_build_executor_drains_only_its_named_queue() -> None:
    executor = build_executor("loops", "worker-0-loops")

    assert executor.queue_names == ["loops"]
    assert executor.interval == pytest.approx(1.0)
    assert executor.worker_id == "worker-0-loops"


def test_lock_retries_inside_one_executor_with_growing_capped_delay() -> None:
    executor = ScriptedExecutor(8)
    delays: list[float] = []

    LockTolerantRun(executor, sleep=delays.append, monotonic=lambda: 0.0).run()

    assert executor.calls == 9
    assert delays[:3] == [0.5, 1.0, 1.0]
    assert len(delays) == 40
    assert sum(delays) == pytest.approx(39.5)


def test_non_lock_error_propagates_without_retry() -> None:
    executor = ScriptedExecutor(1, error="disk I/O error")

    with pytest.raises(OperationalError, match="disk I/O error"):
        LockTolerantRun(executor, sleep=lambda _: None).run()

    assert executor.calls == 1


def test_stop_during_backoff_returns_without_retry() -> None:
    executor = ScriptedExecutor(2)

    def stop(_seconds: float) -> None:
        executor.running = False

    LockTolerantRun(executor, sleep=stop).run()

    assert executor.calls == 1


def test_quiet_run_resets_the_delay() -> None:
    clock = [0.0]
    delays: list[float] = []

    class QuietExecutor(ScriptedExecutor):
        def run(self) -> None:
            if self.calls == 1:
                clock[0] += 61.0
            super().run()

    executor = QuietExecutor(2)
    LockTolerantRun(executor, sleep=delays.append, monotonic=lambda: clock[0]).run()

    assert executor.calls == 3
    assert delays == [0.5, 0.5]


def test_long_lock_streak_repeats_one_line_error_every_ten_minutes(caplog: pytest.LogCaptureFixture) -> None:
    clock = [0.0]
    executor = ScriptedExecutor(4)
    last_waited_call = [0]

    def wait(_seconds: float) -> None:
        if executor.calls != last_waited_call[0]:
            clock[0] += 601.0
            last_waited_call[0] = executor.calls

    LockTolerantRun(executor, sleep=wait, monotonic=lambda: clock[0]).run()

    alerts = [record for record in caplog.records if record.levelname == "ERROR"]
    assert [record.getMessage() for record in alerts] == [
        f"Executor database lock has persisted for {age}s; still retrying" for age in (601, 1202, 1803)
    ]
    assert all(record.exc_info is None for record in alerts)


def test_lock_streak_degraded_state_clears_after_success() -> None:
    clock = [0.0]
    observed: list[bool] = []
    executor = ScriptedExecutor(2)

    def wait(_seconds: float) -> None:
        observed.append(run.degraded)
        clock[0] += 601.0

    run = LockTolerantRun(executor, sleep=wait, monotonic=lambda: clock[0])
    run.run()

    assert observed == [False, True]
    assert not run.degraded


def test_degraded_state_clears_after_quiet_run_without_waiting_for_exit() -> None:
    clock = [0.0]
    observed: list[bool] = []

    class QuietExecutor(ScriptedExecutor):
        def run(self) -> None:
            if self.calls == 2:
                clock[0] += 61.0
                observed.append(run.degraded)
            super().run()

    executor = QuietExecutor(2)

    def wait(_seconds: float) -> None:
        if executor.calls == 1:
            clock[0] += 601.0

    run = LockTolerantRun(executor, sleep=wait, monotonic=lambda: clock[0])
    run.run()

    assert observed == [False]


class TestExecutorThreadConnectionHygiene(TestCase):
    def test_spawned_thread_closes_its_raw_db_handle(self) -> None:
        raw_connections: list[sqlite3.Connection] = []

        class OrmTouchingExecutor:
            running = True

            def run(self) -> None:
                connection.ensure_connection()
                raw_connections.append(connection.connection)

        thread = spawn_executor_thread(OrmTouchingExecutor())
        thread.join(timeout=10)

        assert raw_connections
        with pytest.raises(sqlite3.ProgrammingError):
            raw_connections[0].execute("SELECT 1")
