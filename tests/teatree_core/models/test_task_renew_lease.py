"""``Task.renew_lease`` is a claim-generation compare-and-swap, not a blind write.

The heartbeat must re-stamp the lease ONLY while this worker still owns the
claim. Worker A's lease lapses, worker B reclaims and re-claims the task (a new
``claimed_at`` generation); A's next heartbeat must NOT resurrect its dead claim
— otherwise A and B both drive the same unit (double-spend, racing complete()).
The CAS keys on ``(status=CLAIMED, claimed_by, claimed_by_session, claimed_at)``:
after B's reclaim the predicate matches zero rows and A raises ``LeaseLostError``.
"""

from datetime import timedelta

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, Ticket
from teatree.core.models.errors import LeaseLostError


class TestRenewLeaseCas(TestCase):
    def _claimed_task(self, *, claimed_by: str = "worker-A") -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        task.claim(claimed_by=claimed_by, lease_seconds=300)
        return task

    def test_owner_renews_its_live_claim(self) -> None:
        task = self._claimed_task()
        before = task.lease_expires_at
        task.renew_lease(lease_seconds=600)
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
        assert task.lease_expires_at is not None
        assert before is not None
        assert task.lease_expires_at > before

    def test_stale_owner_does_not_resurrect_a_reclaimed_task(self) -> None:
        # Worker A holds a stale in-memory instance whose lease lapsed.
        task = self._claimed_task(claimed_by="worker-A")
        worker_a = Task.objects.get(pk=task.pk)
        # The lease expires and worker B reclaims + re-claims the row (a new
        # claim generation: fresh claimed_at, claimed_by).
        Task.objects.filter(pk=task.pk).update(lease_expires_at=timezone.now() - timedelta(minutes=5))
        reclaimed = Task.objects.get(pk=task.pk)
        reclaimed.status = Task.Status.PENDING
        reclaimed.claimed_by = ""
        reclaimed.claimed_by_session = ""
        reclaimed.claimed_at = None
        reclaimed.lease_expires_at = None
        reclaimed.save()
        reclaimed.claim(claimed_by="worker-B", lease_seconds=300)

        # Worker A's heartbeat on its stale instance must NOT re-stamp the lease.
        with pytest.raises(LeaseLostError):
            worker_a.renew_lease(lease_seconds=600)

        # The row still belongs to B — A did not resurrect its claim.
        final = Task.objects.get(pk=task.pk)
        assert final.status == Task.Status.CLAIMED
        assert final.claimed_by == "worker-B"

    def test_renew_after_terminal_raises(self) -> None:
        task = self._claimed_task()
        worker = Task.objects.get(pk=task.pk)
        task.complete()
        with pytest.raises(LeaseLostError):
            worker.renew_lease()
