"""``Task.not_before`` — the usage-window park admission gate (Directive #3).

A parked task is returned to the queue as PENDING with a ``not_before`` in the future, so
the claim path skips it until the window re-arms — instead of the task being re-claimed and
re-dispatched into the same 429 in a tight loop.
"""

from datetime import datetime, timedelta

import django.test
import pytest
from django.utils import timezone

from teatree.core.models import Session, Task, Ticket
from teatree.core.models.errors import InvalidTransitionError


def _pending_task(not_before: datetime | None = None) -> Task:
    ticket = Ticket.objects.create(issue_url="https://example.com/i/1", role=Ticket.Role.AUTHOR)
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(
        ticket=ticket,
        session=session,
        phase="coding",
        not_before=not_before,
    )
    # A "coding" phase auto-routes to INTERACTIVE on save (the loop-dispatch chokepoint);
    # force HEADLESS with a direct UPDATE so the not_before gate is exercised on the
    # headless claim path this test targets.
    task.refresh_from_db()
    return task


class TestClaimHonoursNotBefore(django.test.TestCase):
    def test_future_not_before_is_not_claimable(self) -> None:
        now = timezone.now()
        deadline = now + timedelta(hours=5)
        task = _pending_task(not_before=deadline)
        assert Task.objects.claim_next_pending(claimed_by="worker") is None
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
        assert task.not_before == deadline

    def test_a_held_task_keeps_its_deadline_after_direct_claim_refusal(self) -> None:
        deadline = timezone.now() + timedelta(hours=5)
        task = _pending_task(not_before=deadline)

        with pytest.raises(InvalidTransitionError, match="Task parked until"):
            task.claim(claimed_by="worker")

        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
        assert task.not_before == deadline

    def test_elapsed_not_before_is_claimable(self) -> None:
        now = timezone.now()
        task = _pending_task(not_before=now - timedelta(minutes=1))
        claimed = Task.objects.claim_next_pending(claimed_by="worker")
        assert claimed is not None
        assert claimed.pk == task.pk

    def test_null_not_before_is_claimable_byte_identical(self) -> None:
        # An ordinary task (never parked) has a null not_before → claim path unchanged.
        task = _pending_task(not_before=None)
        claimed = Task.objects.claim_next_pending(claimed_by="worker")
        assert claimed is not None
        assert claimed.pk == task.pk

    def test_claimable_skips_future_not_before(self) -> None:
        now = timezone.now()
        _pending_task(not_before=now + timedelta(hours=5))
        assert not Task.objects.claimable().exists()

    def test_claimable_includes_elapsed_not_before(self) -> None:
        now = timezone.now()
        _pending_task(not_before=now - timedelta(minutes=1))
        assert Task.objects.claimable().exists()


class TestPark(django.test.TestCase):
    def test_park_returns_task_to_queue_with_not_before(self) -> None:
        now = timezone.now()
        task = _pending_task()
        task.claim(claimed_by="worker")
        assert task.status == Task.Status.CLAIMED
        reset = now + timedelta(hours=5)
        task.park(not_before=reset)
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
        assert task.not_before == reset
        assert task.claimed_by == ""
        assert task.lease_expires_at is None

    def test_parked_task_is_not_immediately_reclaimable(self) -> None:
        now = timezone.now()
        task = _pending_task()
        task.claim(claimed_by="worker")
        task.park(not_before=now + timedelta(hours=5))
        assert Task.objects.claim_next_pending(claimed_by="worker2") is None
