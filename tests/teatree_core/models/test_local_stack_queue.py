"""The local-stack acquisition queue model (souliane/teatree#44, #2190).

A ``worktree start`` / ``workspace start`` that finds no free slot ENQUEUES a
``LocalStackQueueItem`` instead of exiting 1. A loop scanner drains the queue
each tick with Fibonacci-minute backoff, never tearing down another ticket's
stack. The model carries the durable retry state (status, attempt_count,
next_attempt_at, error_message) and a manager ``due_for_attempt(now)`` the
drainer consults.
"""

from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import LocalStackQueueItem, Ticket, Worktree


def _worktree(*, overlay: str = "t3-heavy", ticket_number: str = "9001") -> Worktree:
    ticket = Ticket.objects.create(
        issue_url=f"https://example.com/{overlay}/issues/{ticket_number}",
        overlay=overlay,
    )
    return Worktree.objects.create(
        overlay=overlay,
        ticket=ticket,
        repo_path="backend",
        branch=f"{ticket_number}-feat",
        state=Worktree.State.PROVISIONED,
    )


class TestLocalStackQueueItemDefaults(TestCase):
    """A freshly-enqueued item starts QUEUED with a zero attempt count."""

    def test_defaults(self) -> None:
        wt = _worktree()
        item = LocalStackQueueItem.objects.create(overlay=wt.overlay, worktree=wt)
        assert item.status == LocalStackQueueItem.Status.QUEUED
        assert item.attempt_count == 0
        assert item.error_message == ""
        assert item.next_attempt_at is None


class TestDueForAttempt(TestCase):
    """``due_for_attempt`` returns the items the drainer should retry now."""

    def test_queued_with_no_next_attempt_is_due(self) -> None:
        wt = _worktree()
        item = LocalStackQueueItem.objects.create(overlay=wt.overlay, worktree=wt)
        due = list(LocalStackQueueItem.objects.due_for_attempt(timezone.now()))
        assert item in due

    def test_retrying_in_the_past_is_due(self) -> None:
        wt = _worktree(ticket_number="9002")
        now = timezone.now()
        item = LocalStackQueueItem.objects.create(
            overlay=wt.overlay,
            worktree=wt,
            status=LocalStackQueueItem.Status.RETRYING,
            attempt_count=1,
            next_attempt_at=now - timedelta(minutes=1),
        )
        assert item in list(LocalStackQueueItem.objects.due_for_attempt(now))

    def test_retrying_in_the_future_is_not_due(self) -> None:
        wt = _worktree(ticket_number="9003")
        now = timezone.now()
        item = LocalStackQueueItem.objects.create(
            overlay=wt.overlay,
            worktree=wt,
            status=LocalStackQueueItem.Status.RETRYING,
            attempt_count=1,
            next_attempt_at=now + timedelta(minutes=5),
        )
        assert item not in list(LocalStackQueueItem.objects.due_for_attempt(now))

    def test_terminal_states_are_never_due(self) -> None:
        now = timezone.now()
        for ticket_number, status in (
            ("9004", LocalStackQueueItem.Status.READY),
            ("9005", LocalStackQueueItem.Status.DONE),
            ("9006", LocalStackQueueItem.Status.DEAD),
        ):
            wt = _worktree(ticket_number=ticket_number)
            item = LocalStackQueueItem.objects.create(overlay=wt.overlay, worktree=wt, status=status)
            assert item not in list(LocalStackQueueItem.objects.due_for_attempt(now))


class TestIdempotentEnqueueConstraint(TestCase):
    """The partial UniqueConstraint forbids two ACTIVE rows for one worktree."""

    def test_second_active_row_for_same_worktree_is_rejected(self) -> None:
        wt = _worktree(ticket_number="9101")
        LocalStackQueueItem.objects.create(overlay=wt.overlay, worktree=wt)
        with transaction.atomic(), pytest.raises(IntegrityError):
            LocalStackQueueItem.objects.create(
                overlay=wt.overlay,
                worktree=wt,
                status=LocalStackQueueItem.Status.RETRYING,
            )

    def test_a_done_row_does_not_block_a_new_active_row(self) -> None:
        """A terminal (DONE) row leaves the worktree free to be re-enqueued."""
        wt = _worktree(ticket_number="9102")
        LocalStackQueueItem.objects.create(
            overlay=wt.overlay,
            worktree=wt,
            status=LocalStackQueueItem.Status.DONE,
        )
        # Must NOT raise — the partial constraint only covers QUEUED/RETRYING.
        fresh = LocalStackQueueItem.objects.create(overlay=wt.overlay, worktree=wt)
        assert fresh.status == LocalStackQueueItem.Status.QUEUED


class TestEnqueueHelper(TestCase):
    """``enqueue`` is the idempotent insertion seam used by the gate."""

    def test_enqueue_creates_a_queued_row(self) -> None:
        wt = _worktree(ticket_number="9201")
        item = LocalStackQueueItem.objects.enqueue(wt)
        assert item.status == LocalStackQueueItem.Status.QUEUED
        assert item.worktree_id == wt.pk

    def test_enqueue_is_idempotent_when_an_active_row_exists(self) -> None:
        wt = _worktree(ticket_number="9202")
        first = LocalStackQueueItem.objects.enqueue(wt)
        second = LocalStackQueueItem.objects.enqueue(wt)
        assert first.pk == second.pk
        active = LocalStackQueueItem.objects.filter(
            worktree=wt,
            status__in=[LocalStackQueueItem.Status.QUEUED, LocalStackQueueItem.Status.RETRYING],
        )
        assert active.count() == 1


class TestScheduleNextAttempt(TestCase):
    """``schedule_next_attempt`` advances the attempt count with Fibonacci backoff."""

    def test_first_retry_waits_one_minute(self) -> None:
        wt = _worktree(ticket_number="9301")
        item = LocalStackQueueItem.objects.create(overlay=wt.overlay, worktree=wt)
        now = timezone.now()
        item.schedule_next_attempt(error="full", now=now)
        item.refresh_from_db()
        assert item.status == LocalStackQueueItem.Status.RETRYING
        assert item.attempt_count == 1
        assert item.error_message == "full"
        # attempt index 1 → fibonacci 1 minute
        assert item.next_attempt_at == now + timedelta(minutes=1)

    def test_backoff_follows_fibonacci_across_attempts(self) -> None:
        wt = _worktree(ticket_number="9302")
        item = LocalStackQueueItem.objects.create(overlay=wt.overlay, worktree=wt)
        now = timezone.now()
        # Each call increments attempt_count (1, 2, 3, 4, 5) and schedules
        # next_attempt_at at fibonacci_minutes(attempt_count): 1, 2, 3, 5, 8.
        expected_minutes = [1, 2, 3, 5, 8]
        for expected in expected_minutes:
            item.schedule_next_attempt(error="full", now=now)
            item.refresh_from_db()
            assert item.next_attempt_at == now + timedelta(minutes=expected)

    def test_exhausting_max_attempts_marks_dead(self) -> None:
        wt = _worktree(ticket_number="9303")
        item = LocalStackQueueItem.objects.create(
            overlay=wt.overlay,
            worktree=wt,
            attempt_count=13,
        )
        item.schedule_next_attempt(error="still full", now=timezone.now(), max_attempts=13)
        item.refresh_from_db()
        assert item.status == LocalStackQueueItem.Status.DEAD


class TestMarkReady(TestCase):
    """``mark_ready`` records a successful acquisition (slot freed, start fired)."""

    def test_mark_ready_sets_status(self) -> None:
        wt = _worktree(ticket_number="9401")
        item = LocalStackQueueItem.objects.create(
            overlay=wt.overlay,
            worktree=wt,
            status=LocalStackQueueItem.Status.RETRYING,
            attempt_count=3,
        )
        item.mark_ready()
        item.refresh_from_db()
        assert item.status == LocalStackQueueItem.Status.READY


class TestMarkDoneAndStr(TestCase):
    """``mark_done`` settles the row; ``__str__`` is informative."""

    def test_mark_done_sets_status(self) -> None:
        wt = _worktree(ticket_number="9501")
        item = LocalStackQueueItem.objects.create(
            overlay=wt.overlay,
            worktree=wt,
            status=LocalStackQueueItem.Status.READY,
        )
        item.mark_done()
        item.refresh_from_db()
        assert item.status == LocalStackQueueItem.Status.DONE

    def test_str_carries_worktree_status_and_attempt(self) -> None:
        wt = _worktree(ticket_number="9502")
        item = LocalStackQueueItem.objects.create(overlay=wt.overlay, worktree=wt)
        rendered = str(item)
        assert str(wt.pk) in rendered
        assert "queued" in rendered
