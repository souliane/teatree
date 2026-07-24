"""Age-based retention pruning for the high-churn control-DB tables (#3693).

The load-bearing invariant is the safety guard: retention NEVER deletes a row of a
non-terminal ticket, a non-terminal task, or a row within the retention window —
over-deleting a referenced/live row is far worse than a bloated DB. Each guard test
is written to go RED if the guard were dropped (the prunable set would then include
the live row). ``apply_retention`` deletes; ``plan_retention`` reports only.
"""

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from teatree.config.settings import UserSettings
from teatree.core.models import IncomingEvent, Session, Task, TaskAttempt, Ticket
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX
from teatree.core.retention import apply_retention, incoming_events_prunable, plan_retention, task_attempts_prunable

_OLD = timezone.now() - dt.timedelta(days=60)
_RECENT = timezone.now() - dt.timedelta(days=2)


def _attempt(
    *,
    ticket_state: str = Ticket.State.MERGED,
    task_status: str = Task.Status.COMPLETED,
    started_at: dt.datetime = _OLD,
    error: str = "",
) -> TaskAttempt:
    ticket = Ticket.objects.create(overlay="acme", state=ticket_state)
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session, status=task_status)
    attempt = TaskAttempt.objects.create(task=task, error=error)
    # started_at is auto_now_add — age it with a direct UPDATE.
    TaskAttempt.objects.filter(pk=attempt.pk).update(started_at=started_at)
    return TaskAttempt.objects.get(pk=attempt.pk)


def _event(
    *,
    idempotency_key: str,
    received_at: dt.datetime = _OLD,
    processed: bool = True,
    dead_lettered: bool = False,
) -> IncomingEvent:
    return IncomingEvent.objects.create(
        source=IncomingEvent.Source.SLACK,
        idempotency_key=idempotency_key,
        received_at=received_at,
        processed_at=timezone.now() if processed else None,
        dead_lettered_at=timezone.now() if dead_lettered else None,
    )


class TaskAttemptPrunableGuardTestCase(TestCase):
    def test_old_terminal_owned_attempt_is_prunable(self) -> None:
        attempt = _attempt()
        prunable = task_attempts_prunable(timezone.now() - dt.timedelta(days=30))
        assert list(prunable.values_list("pk", flat=True)) == [attempt.pk]

    def test_never_prunes_attempt_of_non_terminal_ticket(self) -> None:
        # The critical guard: a live ticket's attempt must survive even when old
        # and its task is terminal — deleting it drops history a live ticket needs.
        _attempt(ticket_state=Ticket.State.STARTED, task_status=Task.Status.COMPLETED)
        prunable = task_attempts_prunable(timezone.now() - dt.timedelta(days=30))
        assert prunable.count() == 0

    def test_never_prunes_attempt_of_non_terminal_task(self) -> None:
        _attempt(ticket_state=Ticket.State.MERGED, task_status=Task.Status.PENDING)
        prunable = task_attempts_prunable(timezone.now() - dt.timedelta(days=30))
        assert prunable.count() == 0

    def test_never_prunes_attempt_within_window(self) -> None:
        _attempt(started_at=_RECENT)
        prunable = task_attempts_prunable(timezone.now() - dt.timedelta(days=30))
        assert prunable.count() == 0

    def test_shipped_ticket_is_not_prunable(self) -> None:
        # SHIPPED is excluded on purpose — its PR is still open and may re-work.
        _attempt(ticket_state=Ticket.State.SHIPPED)
        assert task_attempts_prunable(timezone.now() - dt.timedelta(days=30)).count() == 0


class IncomingEventPrunableGuardTestCase(TestCase):
    def test_old_processed_event_is_prunable(self) -> None:
        event = _event(idempotency_key="k-processed")
        prunable = incoming_events_prunable(timezone.now() - dt.timedelta(days=30))
        assert list(prunable.values_list("pk", flat=True)) == [event.pk]

    def test_old_dead_lettered_event_is_prunable(self) -> None:
        event = _event(idempotency_key="k-dead", processed=False, dead_lettered=True)
        prunable = incoming_events_prunable(timezone.now() - dt.timedelta(days=30))
        assert list(prunable.values_list("pk", flat=True)) == [event.pk]

    def test_never_prunes_old_unprocessed_event(self) -> None:
        # An un-processed, non-dead-lettered event is still in-flight — never pruned.
        _event(idempotency_key="k-inflight", processed=False, dead_lettered=False)
        assert incoming_events_prunable(timezone.now() - dt.timedelta(days=30)).count() == 0

    def test_never_prunes_processed_event_within_window(self) -> None:
        _event(idempotency_key="k-recent", received_at=_RECENT)
        assert incoming_events_prunable(timezone.now() - dt.timedelta(days=30)).count() == 0


class PlanRetentionTestCase(TestCase):
    def test_plan_reports_without_deleting(self) -> None:
        _attempt()
        _event(idempotency_key="k1")
        plan = plan_retention()
        assert plan.applied is False
        assert plan.total_rows == 2
        assert TaskAttempt.objects.count() == 1
        assert IncomingEvent.objects.count() == 1

    def test_plan_counts_park_junk_subset(self) -> None:
        _attempt(error=f"{LIMIT_PARKED_PREFIX}weekly window exhausted")
        _attempt(error="boom: a genuine crash")
        plan = plan_retention()
        (attempts,) = (t for t in plan.tables if t.table == "TaskAttempt")
        assert attempts.rows == 2
        assert attempts.junk == 1

    def test_zero_window_disables_table(self) -> None:
        _attempt()
        settings = UserSettings(task_attempt_retention_days=0)
        plan = plan_retention(settings=settings)
        (attempts,) = (t for t in plan.tables if t.table == "TaskAttempt")
        assert attempts.disabled is True
        assert attempts.rows == 0


class ApplyRetentionTestCase(TestCase):
    def test_apply_deletes_only_prunable_rows(self) -> None:
        old = _attempt()
        live = _attempt(ticket_state=Ticket.State.STARTED)
        recent = _attempt(started_at=_RECENT)
        old_event = _event(idempotency_key="k-old")
        inflight = _event(idempotency_key="k-inflight", processed=False)

        plan = apply_retention()

        assert plan.applied is True
        assert plan.total_rows == 2
        assert not TaskAttempt.objects.filter(pk=old.pk).exists()
        assert TaskAttempt.objects.filter(pk=live.pk).exists()
        assert TaskAttempt.objects.filter(pk=recent.pk).exists()
        assert not IncomingEvent.objects.filter(pk=old_event.pk).exists()
        assert IncomingEvent.objects.filter(pk=inflight.pk).exists()

    def test_apply_with_zero_window_deletes_nothing(self) -> None:
        _attempt()
        _event(idempotency_key="k1")
        settings = UserSettings(task_attempt_retention_days=0, incoming_event_retention_days=0)
        plan = apply_retention(settings=settings)
        assert plan.total_rows == 0
        assert TaskAttempt.objects.count() == 1
        assert IncomingEvent.objects.count() == 1
