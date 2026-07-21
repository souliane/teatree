"""The extracted ``task_claim.claim`` / ``task_claim.renew_lease`` compare-and-swaps.

The claim/lease helpers live in ``teatree.core.models.task_claim`` and the thin
``Task`` methods delegate to them. These tests call the module functions directly
(the public symbols the extraction introduced) so the CAS contract is pinned at
its own seam: a fresh row claims, a live-lease row is not stolen, a terminal row
is not re-claimed, an expired-lease orphan is reclaimable, and a heartbeat renews
only while this worker still owns the claim generation.
"""

from datetime import timedelta

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, Ticket
from teatree.core.models.errors import InvalidTransitionError, LeaseLostError
from teatree.core.models.task_claim import claim, renew_lease


class TestClaim(TestCase):
    def _pending_task(self) -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        return Task.objects.create(ticket=ticket, session=session, phase="coding")

    def test_claims_a_fresh_pending_row(self) -> None:
        task = self._pending_task()
        claim(task, claimed_by="worker", claimed_by_session="sess", lease_seconds=300)
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
        assert task.claimed_by == "worker"
        assert task.claimed_by_session == "sess"
        assert task.lease_expires_at is not None

    def test_live_claim_is_not_stolen(self) -> None:
        task = self._pending_task()
        claim(task, claimed_by="owner", lease_seconds=300)
        contender = Task.objects.get(pk=task.pk)
        with pytest.raises(InvalidTransitionError, match="already claimed"):
            claim(contender, claimed_by="thief", lease_seconds=300)
        contender.refresh_from_db()
        assert contender.claimed_by == "owner"

    def test_terminal_row_is_not_reclaimed(self) -> None:
        task = self._pending_task()
        claim(task, claimed_by="owner", lease_seconds=300)
        task.complete()
        revived = Task.objects.get(pk=task.pk)
        with pytest.raises(InvalidTransitionError, match="already finished"):
            claim(revived, claimed_by="thief", lease_seconds=300)

    def test_expired_lease_orphan_is_reclaimable(self) -> None:
        task = self._pending_task()
        claim(task, claimed_by="dead-owner", lease_seconds=300)
        Task.objects.filter(pk=task.pk).update(lease_expires_at=timezone.now() - timedelta(minutes=5))
        orphan = Task.objects.get(pk=task.pk)
        claim(orphan, claimed_by="new-owner", claimed_by_session="fresh", lease_seconds=300)
        orphan.refresh_from_db()
        assert orphan.status == Task.Status.CLAIMED
        assert orphan.claimed_by == "new-owner"
        assert orphan.claimed_by_session == "fresh"


class TestRenewLease(TestCase):
    def _claimed_task(self, *, claimed_by: str = "worker") -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        claim(task, claimed_by=claimed_by, lease_seconds=300)
        return task

    def test_owner_renews_its_live_claim(self) -> None:
        task = self._claimed_task()
        before = task.lease_expires_at
        renew_lease(task, lease_seconds=600)
        task.refresh_from_db()
        assert before is not None
        assert task.lease_expires_at is not None
        assert task.lease_expires_at > before

    def test_stale_generation_raises_lease_lost(self) -> None:
        task = self._claimed_task(claimed_by="worker-A")
        worker_a = Task.objects.get(pk=task.pk)
        Task.objects.filter(pk=task.pk).update(lease_expires_at=timezone.now() - timedelta(minutes=5))
        reclaimed = Task.objects.get(pk=task.pk)
        reclaimed.status = Task.Status.PENDING
        reclaimed.claimed_by = ""
        reclaimed.claimed_at = None
        reclaimed.lease_expires_at = None
        reclaimed.save()
        claim(reclaimed, claimed_by="worker-B", lease_seconds=300)
        with pytest.raises(LeaseLostError):
            renew_lease(worker_a, lease_seconds=600)
