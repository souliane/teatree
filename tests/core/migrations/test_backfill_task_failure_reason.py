# test-path: cross-cutting
"""The 0049 columns are recovered from the attempt rows that always held the text.

A data migration that runs and changes nothing looks identical to one that worked, so
each case is paired: what it MUST repair, and what it must leave alone. The leave-alone
half is what stops a future "just stamp a default" from passing this file.
"""

import importlib

from django.apps import apps
from django.test import TestCase

from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Session, Task, TaskAttempt, Ticket

_MIGRATION = importlib.import_module("teatree.core.migrations.0054_backfill_task_failure_reason")


def _run_backfill() -> None:
    _MIGRATION.backfill(apps, None)


def _task(*, status: str, reason: str = "", kind: str = "") -> Task:
    ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
    session = Session.objects.create(ticket=ticket, agent_id="coding")
    return Task.objects.create(
        ticket=ticket, session=session, phase="coding", status=status, failure_reason=reason, failure_kind=kind
    )


class TestBackfillRecoversTheReasonFromTheAttempt(TestCase):
    def _failed_task(self, *, reason: str = "", kind: str = "") -> Task:
        return _task(status=Task.Status.FAILED, reason=reason, kind=kind)

    def test_a_blank_reason_is_recovered_from_the_attempt_error(self) -> None:
        task = self._failed_task()
        TaskAttempt.objects.create(task=task, error="lease_expired: lease held by another worker")

        _run_backfill()

        task.refresh_from_db()
        assert task.failure_reason == "lease_expired: lease held by another worker"
        assert task.failure_kind == FailureKind.LEASE_EXPIRED

    def test_the_newest_non_blank_attempt_wins(self) -> None:
        task = self._failed_task()
        TaskAttempt.objects.create(task=task, error="first failure")
        TaskAttempt.objects.create(task=task, error="")
        TaskAttempt.objects.create(task=task, error="the reason it finally failed")

        _run_backfill()

        task.refresh_from_db()
        assert task.failure_reason == "the reason it finally failed", "a later blank attempt must not mask the cause"

    def test_a_task_whose_attempts_recorded_nothing_keeps_its_blank_reason(self) -> None:
        # Inventing a cause is worse than admitting none — UNRECORDED exists to name this.
        task = self._failed_task()
        TaskAttempt.objects.create(task=task, error="")

        _run_backfill()

        task.refresh_from_db()
        assert task.failure_reason == ""

    def test_a_reason_already_recorded_is_never_overwritten(self) -> None:
        # Anti-vacuity: a backfill that stamped every failed row would pass the first case
        # and destroy the live path's own verdict here.
        task = self._failed_task(reason="the live path recorded this", kind=FailureKind.RUNTIME_CEILING)
        TaskAttempt.objects.create(task=task, error="a stale attempt error")

        _run_backfill()

        task.refresh_from_db()
        assert task.failure_reason == "the live path recorded this"
        assert task.failure_kind == FailureKind.RUNTIME_CEILING

    def test_a_task_that_did_not_fail_is_untouched(self) -> None:
        task = _task(status=Task.Status.COMPLETED)
        TaskAttempt.objects.create(task=task, error="a warning that is not a failure")

        _run_backfill()

        task.refresh_from_db()
        assert task.failure_reason == "", "only FAILED rows carry a failure reason"


class TestBackfillClassifiesTheAttemptRows(TestCase):
    def test_an_attempt_holding_an_error_gets_its_kind(self) -> None:
        task = _task(status=Task.Status.FAILED)
        attempt = TaskAttempt.objects.create(task=task, error="lease_expired: reaped")

        _run_backfill()

        attempt.refresh_from_db()
        assert attempt.failure_kind == FailureKind.LEASE_EXPIRED

    def test_an_attempt_with_no_error_is_left_blank(self) -> None:
        task = _task(status=Task.Status.FAILED)
        attempt = TaskAttempt.objects.create(task=task, error="")

        _run_backfill()

        attempt.refresh_from_db()
        assert attempt.failure_kind == "", "no text means no verdict to derive"
