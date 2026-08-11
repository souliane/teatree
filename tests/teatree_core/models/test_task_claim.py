"""The extracted ``task_claim.claim`` / ``task_claim.renew_lease`` compare-and-swaps.

The claim/lease helpers live in ``teatree.core.models.task_claim`` and the thin
``Task`` methods delegate to them. These tests call the module functions directly
(the public symbols the extraction introduced) so the CAS contract is pinned at
its own seam: a fresh row claims, a live-lease row is not stolen, a terminal row
is not re-claimed, an expired-lease orphan is reclaimable, and a heartbeat renews
only while this worker still owns the claim generation.
"""

from datetime import timedelta
from unittest import mock

import pytest
from django.db import OperationalError
from django.test import TestCase
from django.utils import timezone

from teatree.core.claim_liveness import reset_driving_registry
from teatree.core.models import Session, Task, Ticket
from teatree.core.models.errors import InvalidTransitionError, LeaseLostError
from teatree.core.models.task_claim import claim, describe_lease_loss, drive_claim, renew_lease


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


class TestDriveClaim(TestCase):
    """#4164 follow-up: drive_claim's cross-process marker.

    Pairs the in-memory registry with a persisted, cross-process-visible
    ``owner_driving_since`` marker.
    """

    def _claimed_task(self) -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        claim(task, claimed_by="worker", lease_seconds=300)
        return task

    def tearDown(self) -> None:
        reset_driving_registry()

    def test_sets_owner_driving_since_for_the_duration(self) -> None:
        task = self._claimed_task()
        with drive_claim(task):
            task.refresh_from_db()
            assert task.owner_driving_since is not None
        task.refresh_from_db()
        assert task.owner_driving_since is None

    def test_clears_owner_driving_since_even_when_the_drive_raises(self) -> None:
        task = self._claimed_task()
        boom = RuntimeError("drive died")
        with pytest.raises(RuntimeError, match="drive died"), drive_claim(task):
            raise boom
        task.refresh_from_db()
        assert task.owner_driving_since is None


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


class TestDescribeLeaseLoss(TestCase):
    """A lost lease names what ACTUALLY took the claim (#3982).

    "re-claimed by another worker" was recorded unconditionally, so a self-inflicted
    reclaim — the worker's own ``reclaim_orphaned_claims`` sweep requeueing a task whose
    starved heartbeat let the lease lapse — sent the operator hunting for a competing
    worker that does not exist.
    """

    def _claimed_task(self, *, claimed_by: str = "worker-A") -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        task = Task.objects.create(ticket=ticket, session=session, phase="shipping")
        claim(task, claimed_by=claimed_by, lease_seconds=300)
        return task

    def test_a_requeued_row_reads_as_an_in_process_reclaim(self) -> None:
        task = self._claimed_task()
        Task.objects.filter(pk=task.pk).update(status=Task.Status.PENDING, claimed_by="", claimed_at=None)

        reason = describe_lease_loss(task)

        assert "in-process" in reason
        assert "re-claimed by a competing worker" not in reason

    def test_a_re_claim_by_the_same_worker_reads_as_in_process(self) -> None:
        task = self._claimed_task(claimed_by="worker-A")
        Task.objects.filter(pk=task.pk).update(claimed_at=timezone.now() + timedelta(seconds=1))

        reason = describe_lease_loss(task)

        assert "in-process" in reason

    def test_a_rival_worker_is_named_as_a_competing_worker(self) -> None:
        task = self._claimed_task(claimed_by="worker-A")
        Task.objects.filter(pk=task.pk).update(claimed_by="worker-B")

        reason = describe_lease_loss(task)

        assert "competing worker" in reason
        assert "worker-B" in reason

    def test_a_terminal_row_names_its_terminal_status(self) -> None:
        task = self._claimed_task()
        Task.objects.filter(pk=task.pk).update(status=Task.Status.COMPLETED)

        assert "completed" in describe_lease_loss(task)

    def test_every_reason_keeps_the_lease_lost_phrase_the_taxonomy_keys_on(self) -> None:
        # ``stuck_loop: lease lost`` is what classify_failure() matches on for
        # FailureKind.LEASE_LOST — a reworded reason must not fall out of the taxonomy.
        task = self._claimed_task()
        Task.objects.filter(pk=task.pk).update(status=Task.Status.PENDING, claimed_by="")

        assert describe_lease_loss(task).startswith(f"lease lost for task {task.pk}:")

    def test_a_deleted_row_says_so_rather_than_naming_a_reclaimer(self) -> None:
        task = self._claimed_task()
        Task.objects.filter(pk=task.pk).delete()

        assert "no longer exists" in describe_lease_loss(task)

    def test_an_unreadable_row_degrades_instead_of_raising(self) -> None:
        # The read-back contends with the very writer that reclaimed the row, so a lock
        # error here is the EXPECTED failure. Raising it would escape the caller's
        # `except LeaseLostError`, the heartbeat's generic handler would log and keep
        # driving, and the abort would be lost — two drivers on one unit.
        task = self._claimed_task()
        locked = OperationalError("database table is locked: teatree_task")
        with mock.patch.object(Task.objects, "filter", side_effect=locked):
            reason = describe_lease_loss(task)

        assert reason.startswith(f"lease lost for task {task.pk}:")
        assert "could not be read back" in reason
        assert "OperationalError" in reason


class TestTakingAClaimClearsThePreviousDriveMarker(TestCase):
    """A claim TAKE stamps this process as the owner, so it must not inherit the last one's marker.

    ``owner_driving_since`` paired with the NEW owner's live pid reads as "this process is
    driving it" to ``owner_is_executing`` — before the new holder has entered ``drive_claim``
    at all — so every sweep withholds its reap on evidence about a run that already ended.
    """

    def _task_with_a_stale_marker(self) -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        Task.objects.filter(pk=task.pk).update(owner_driving_since=timezone.now() - timedelta(hours=1))
        return Task.objects.get(pk=task.pk)

    def test_the_single_row_claim_clears_it(self) -> None:
        task = self._task_with_a_stale_marker()

        claim(task, claimed_by="worker", lease_seconds=300)

        assert Task.objects.get(pk=task.pk).owner_driving_since is None

    def test_the_queryset_claim_clears_it(self) -> None:
        task = self._task_with_a_stale_marker()

        claimed = Task.objects.claim_next_pending(claimed_by="worker")

        assert claimed is not None
        assert Task.objects.get(pk=task.pk).owner_driving_since is None
