"""Transient-``database is locked`` resilience for the canonical-DB writes.

``souliane/teatree#1520``: under concurrent canonical-DB writers (the durable
loop's merge ceremony running alongside fix-agents) a transient
``OperationalError: database is locked`` could escape and abort the merge
keystone mid-flight. ``retry_on_locked`` wraps a DB-write callable in a bounded
retry-on-locked (exponential backoff, small cap) so a momentary lock blocks-
then-proceeds instead of crashing; a non-transient ``OperationalError`` and a
genuinely stuck lock still surface.

Only the unstoppable external — the clock (``time.sleep``) — is stubbed; the
retry control flow and the exception classification are exercised for real.
"""

import tempfile
from collections.abc import Callable
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import pytest
from django.db import OperationalError, connection, transaction
from django.test import TransactionTestCase

from teatree.agents import runner
from teatree.agents.attempt_recorder import record_result_envelope
from teatree.core.modelkit import db_retry
from teatree.core.modelkit.db_retry import retry_on_locked, wait_out_locked_writes
from teatree.core.models import Task, TaskAttempt, Worktree
from teatree.core.worktree.occupancy import occupancy_holder
from tests._file_backed_db import file_backed_default_database, writer_holding_the_lock
from tests.factories import SessionFactory, TicketFactory, WorktreeFactory


class _Flaky:
    """A callable that raises ``database is locked`` for the first ``n`` calls."""

    def __init__(self, fail_times: int, *, exc: Exception | None = None) -> None:
        self.fail_times = fail_times
        self.exc = exc or OperationalError("database is locked")
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return "ok"


class TestRetryOnLocked:
    def test_returns_value_without_retry_when_no_lock(self) -> None:
        flaky = _Flaky(fail_times=0)
        with patch("teatree.core.modelkit.db_retry.time.sleep") as sleep:
            assert retry_on_locked(flaky) == "ok"
        assert flaky.calls == 1
        sleep.assert_not_called()

    def test_retries_past_a_transient_lock_and_succeeds(self) -> None:
        flaky = _Flaky(fail_times=2)
        with patch("teatree.core.modelkit.db_retry.time.sleep") as sleep:
            assert retry_on_locked(flaky, attempts=5, base_delay=0.01) == "ok"
        assert flaky.calls == 3
        assert sleep.call_count == 2

    def test_backoff_is_exponential(self) -> None:
        flaky = _Flaky(fail_times=3)
        with patch("teatree.core.modelkit.db_retry.time.sleep") as sleep:
            retry_on_locked(flaky, attempts=5, base_delay=0.1)
        delays = [call.args[0] for call in sleep.call_args_list]
        assert delays == [0.1, 0.2, 0.4]

    def test_stuck_lock_surfaces_after_the_cap(self) -> None:
        flaky = _Flaky(fail_times=100)
        with (
            patch("teatree.core.modelkit.db_retry.time.sleep"),
            pytest.raises(OperationalError, match="database is locked"),
        ):
            retry_on_locked(flaky, attempts=3, base_delay=0.01)
        assert flaky.calls == 3

    def test_non_transient_operational_error_is_not_retried(self) -> None:
        flaky = _Flaky(fail_times=1, exc=OperationalError("no such table: teatree_merge_clear"))
        with (
            patch("teatree.core.modelkit.db_retry.time.sleep") as sleep,
            pytest.raises(OperationalError, match="no such table"),
        ):
            retry_on_locked(flaky, attempts=5, base_delay=0.01)
        assert flaky.calls == 1
        sleep.assert_not_called()

    def test_unrelated_exception_propagates_immediately(self) -> None:
        flaky = _Flaky(fail_times=1, exc=ValueError("boom"))
        with patch("teatree.core.modelkit.db_retry.time.sleep") as sleep, pytest.raises(ValueError, match="boom"):
            retry_on_locked(flaky, attempts=5, base_delay=0.01)
        assert flaky.calls == 1
        sleep.assert_not_called()


class _LockedStatement:
    """An execute wrapper failing *statement* with *error* for the first *times* calls."""

    def __init__(self, error: str = "database is locked", *, times: int = 1, statement: str = "SELECT 1") -> None:
        self.error = error
        self.times = times
        self.statement = statement
        self.calls = 0

    def __call__(self, execute: Callable[..., object], sql: str, params: object, *forwarded: object) -> object:
        if sql != self.statement:
            return execute(sql, params, *forwarded)
        self.calls += 1
        if self.calls > 100:
            msg = "the scope never gave up"
            raise AssertionError(msg)
        if self.calls <= self.times:
            raise OperationalError(self.error)
        return execute(sql, params, *forwarded)


class TestWaitOutLockedWrites(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.database = self.enterContext(file_backed_default_database())
        connection.ensure_connection()
        connection.connection.execute("PRAGMA busy_timeout = 50")
        self.enterContext(patch.multiple(db_retry, _FIRST_DELAY=0.02, _MAX_DELAY=0.05, _WAIT_BUDGET=3.0))
        self.ticket = TicketFactory()
        self.task = Task.objects.create(
            ticket=self.ticket,
            session=SessionFactory(ticket=self.ticket),
            status=Task.Status.CLAIMED,
            claimed_by="worker-A",
        )

    def _select_one(self) -> None:
        connection.cursor().execute("SELECT 1")

    def test_a_finished_run_is_recorded_once_the_lock_clears(self) -> None:
        with writer_holding_the_lock(self.database, seconds=0.4):
            attempt = record_result_envelope(self.task, {"summary": "finished while the database was locked"})

        self.task.refresh_from_db()
        assert attempt.exit_code == 0
        assert self.task.status == Task.Status.COMPLETED
        assert TaskAttempt.objects.filter(task=self.task).count() == 1

    def test_a_dispatch_brackets_and_releases_the_task_once_the_lock_clears(self) -> None:
        checkout = Path(tempfile.mkdtemp())
        self.addCleanup(checkout.rmdir)
        worktree = WorktreeFactory(ticket=self.ticket, extra={"worktree_path": str(checkout)})
        bracketed: list[object] = []

        def drive(task: Task, **_: object) -> mock.Mock:
            bracketed.append(Task.objects.get(pk=task.pk).owner_driving_since)
            return mock.Mock(spec=TaskAttempt)

        with (
            mock.patch.object(runner, "_run_agent", drive),
            mock.patch.object(runner, "verify_task_checkout"),
            writer_holding_the_lock(self.database, seconds=0.4),
        ):
            attempt = runner.run_agent(self.task, phase="coding", overlay_skill_metadata=mock.Mock())

        assert attempt is not None
        assert bracketed[0] is not None
        assert Task.objects.get(pk=self.task.pk).owner_driving_since is None
        assert occupancy_holder(Worktree.objects.get(pk=worktree.pk)) is None

    def test_a_lock_error_is_retried_until_it_clears(self) -> None:
        locked = _LockedStatement(times=2)
        with wait_out_locked_writes(), connection.execute_wrapper(locked):
            self._select_one()
        assert locked.calls == 3

    def test_the_wait_doubles_up_to_its_cap(self) -> None:
        locked = _LockedStatement(times=4)
        with (
            patch("teatree.core.modelkit.db_retry.time.sleep") as sleep,
            wait_out_locked_writes(),
            connection.execute_wrapper(locked),
        ):
            self._select_one()
        assert [call.args[0] for call in sleep.call_args_list] == [0.02, 0.04, 0.05, 0.05]

    def test_a_lock_error_inside_an_open_transaction_surfaces_at_once(self) -> None:
        locked = _LockedStatement()
        with (
            wait_out_locked_writes(),
            connection.execute_wrapper(locked),
            transaction.atomic(),
            pytest.raises(OperationalError, match="database is locked"),
        ):
            self._select_one()
        assert locked.calls == 1

    def test_any_other_operational_error_surfaces_at_once(self) -> None:
        broken = _LockedStatement("no such table: teatree_task")
        with (
            wait_out_locked_writes(),
            connection.execute_wrapper(broken),
            pytest.raises(OperationalError, match="no such table"),
        ):
            self._select_one()
        assert broken.calls == 1

    def test_a_batched_statement_is_never_re_issued(self) -> None:
        batch = "UPDATE teatree_task SET id = id WHERE 0"
        locked = _LockedStatement(statement=batch)
        with (
            wait_out_locked_writes(),
            connection.execute_wrapper(locked),
            pytest.raises(OperationalError, match="database is locked"),
        ):
            connection.cursor().executemany(batch, [()])
        assert locked.calls == 1

    def test_a_lock_that_never_clears_surfaces_within_the_budget(self) -> None:
        stuck = _LockedStatement(times=1000)
        with (
            patch.object(db_retry, "_WAIT_BUDGET", 0.2),
            wait_out_locked_writes(),
            connection.execute_wrapper(stuck),
            pytest.raises(OperationalError, match="database is locked"),
        ):
            self._select_one()
        assert 1 < stuck.calls < 100

    def test_a_nested_scope_adds_no_second_wrapper(self) -> None:
        outside = len(connection.execute_wrappers)
        with wait_out_locked_writes():
            with wait_out_locked_writes():
                assert len(connection.execute_wrappers) == outside + 1
            assert len(connection.execute_wrappers) == outside + 1
        assert len(connection.execute_wrappers) == outside
