"""``TaskAttempt.Lane`` attribution and the per-row ``effective_tokens`` metric."""

import datetime as dt

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt, Ticket


class TestTaskAttemptEffectiveTokens(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()
        cls.session = Session.objects.create(ticket=cls.ticket)
        cls.task = Task.objects.create(ticket=cls.ticket, session=cls.session)

    def test_none_when_no_tokens_captured(self) -> None:
        attempt = TaskAttempt.objects.create(task=self.task)
        assert attempt.effective_tokens is None

    def test_computed_from_captured_tokens(self) -> None:
        attempt = TaskAttempt.objects.create(
            task=self.task,
            model="claude-opus-4-8",
            input_tokens=1000,
            output_tokens=100,
            cache_read_tokens=2000,
        )
        # opus (m=1.0): 1000 + 0.1*2000 + 4*100 = 1600.
        assert attempt.effective_tokens == pytest.approx(1600.0)

    def test_zero_tokens_still_computes_zero_not_none(self) -> None:
        # An explicit 0 differs from "never captured" — a real, billed
        # zero-token attempt reports 0.0, not None.
        attempt = TaskAttempt.objects.create(
            task=self.task,
            model="opus",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
        )
        assert attempt.effective_tokens == pytest.approx(0.0)


class TestTaskAttemptLane(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()
        cls.session = Session.objects.create(ticket=cls.ticket)
        cls.task = Task.objects.create(ticket=cls.ticket, session=cls.session)

    def test_defaults_to_blank(self) -> None:
        attempt = TaskAttempt.objects.create(task=self.task)
        assert attempt.lane == ""

    def test_stores_subscription_and_metered_choices(self) -> None:
        subscription = TaskAttempt.objects.create(task=self.task, lane=TaskAttempt.Lane.SUBSCRIPTION)
        metered = TaskAttempt.objects.create(task=self.task, lane=TaskAttempt.Lane.METERED)
        subscription.refresh_from_db()
        metered.refresh_from_db()
        assert subscription.lane == "subscription"
        assert metered.lane == "metered"


class TestTaskAttemptPrunable(TestCase):
    """``TaskAttemptQuerySet.prunable`` — the conservative double-guard prune set (#3693)."""

    def _attempt(self, *, ticket_state: str, task_status: str, started_at: dt.datetime) -> TaskAttempt:
        ticket = Ticket.objects.create(overlay="acme", state=ticket_state)
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, status=task_status)
        attempt = TaskAttempt.objects.create(task=task)
        TaskAttempt.objects.filter(pk=attempt.pk).update(started_at=started_at)
        return TaskAttempt.objects.get(pk=attempt.pk)

    def test_old_terminal_owned_attempt_is_prunable(self) -> None:
        old = timezone.now() - dt.timedelta(days=60)
        attempt = self._attempt(ticket_state=Ticket.State.MERGED, task_status=Task.Status.COMPLETED, started_at=old)
        cutoff = timezone.now() - dt.timedelta(days=30)
        assert list(TaskAttempt.objects.prunable(cutoff).values_list("pk", flat=True)) == [attempt.pk]

    def test_never_prunes_live_ticket_or_active_task_or_recent_attempt(self) -> None:
        old = timezone.now() - dt.timedelta(days=60)
        recent = timezone.now() - dt.timedelta(days=2)
        self._attempt(ticket_state=Ticket.State.STARTED, task_status=Task.Status.COMPLETED, started_at=old)
        self._attempt(ticket_state=Ticket.State.MERGED, task_status=Task.Status.PENDING, started_at=old)
        self._attempt(ticket_state=Ticket.State.SHIPPED, task_status=Task.Status.COMPLETED, started_at=old)
        self._attempt(ticket_state=Ticket.State.MERGED, task_status=Task.Status.COMPLETED, started_at=recent)
        assert TaskAttempt.objects.prunable(timezone.now() - dt.timedelta(days=30)).count() == 0


class TestTaskAttemptOutcome(TestCase):
    """``TaskAttempt.outcome`` is stamped from ``exit_code`` + ``error`` on save (#16)."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()
        cls.session = Session.objects.create(ticket=cls.ticket)
        cls.task = Task.objects.create(ticket=cls.ticket, session=cls.session)

    def test_in_flight_attempt_has_blank_outcome(self) -> None:
        # No exit_code recorded yet — neither success nor failure.
        attempt = TaskAttempt.objects.create(task=self.task)
        attempt.refresh_from_db()
        assert attempt.outcome == ""

    def test_clean_exit_is_success(self) -> None:
        attempt = TaskAttempt.objects.create(task=self.task, exit_code=0, error="")
        attempt.refresh_from_db()
        assert attempt.outcome == TaskAttempt.Outcome.SUCCESS

    def test_exit0_with_error_is_refusal(self) -> None:
        # The envelope-refusal fingerprint: a clean exit code but an error string.
        attempt = TaskAttempt.objects.create(task=self.task, exit_code=0, error="refused: missing evidence")
        attempt.refresh_from_db()
        assert attempt.outcome == TaskAttempt.Outcome.REFUSAL

    def test_nonzero_exit_is_crash(self) -> None:
        attempt = TaskAttempt.objects.create(task=self.task, exit_code=1, error="boom")
        attempt.refresh_from_db()
        assert attempt.outcome == TaskAttempt.Outcome.CRASH

    def test_outcome_restamped_when_terminal_fields_written_after_insert(self) -> None:
        # The common lifecycle: the row is inserted in flight (blank outcome),
        # then the terminal exit_code/error are written on completion — the
        # discriminator must be recomputed on that later save, not only on insert.
        attempt = TaskAttempt.objects.create(task=self.task)
        assert attempt.outcome == ""
        attempt.exit_code = 0
        attempt.error = "refused: policy"
        attempt.save()
        attempt.refresh_from_db()
        assert attempt.outcome == TaskAttempt.Outcome.REFUSAL


class TestTaskAttemptQuerySetUsagesCarriesLane(TestCase):
    def test_usages_carries_lane_through_to_attempt_usage(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)
        TaskAttempt.objects.create(
            task=task,
            model="opus",
            input_tokens=100,
            lane=TaskAttempt.Lane.METERED,
        )
        [usage] = TaskAttempt.objects.usages()
        assert usage.lane == "metered"
