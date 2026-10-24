"""#4164: the lease sweeps must not reap a claim whose owner process is still executing it."""

import os
from datetime import datetime, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

import teatree.utils.singleton as singleton_mod
from teatree.core import loop_lease_liveness as liveness
from teatree.core.claim_liveness import driving
from teatree.core.modelkit.task_failure_taxonomy import CANCELLED_PREFIX, PLAN_STALE_PREFIX, FailureKind
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from tests.teatree_core.test_claim_liveness import _READER_NS, pinned_reader_namespace

_LAPSED = timedelta(seconds=120)


class LeaseSweepCase(TestCase):
    """A CLAIMED task whose lease lapsed — the state all three sweeps read as a dead worker."""

    def lapsed_claim(
        self,
        *,
        owner_pid: int | None = None,
        namespace: str = _READER_NS,
        driving_since: datetime | None = None,
        phase: str = "coding",
    ) -> Task:
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR)
        now = timezone.now()
        return Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket, overlay="test"),
            phase=phase,
            status=Task.Status.CLAIMED,
            claimed_by="headless-worker",
            claimed_at=now - _LAPSED,
            heartbeat_at=now - _LAPSED,
            lease_expires_at=now - _LAPSED,
            owner_pid=owner_pid,
            owner_pid_namespace=namespace if owner_pid is not None else "",
            owner_driving_since=driving_since,
        )

    def setUp(self) -> None:
        self.enterContext(pinned_reader_namespace())


class TestReclaimOrphanedClaims(LeaseSweepCase):
    @override_settings(MAX_PHASE_ITERATIONS=5)
    def test_restarted_worker_reclaims_only_phases_with_work_budget(self) -> None:
        coding = self.lapsed_claim(owner_pid=os.getpid() + 1)
        reviewing = self.lapsed_claim(owner_pid=os.getpid() + 1, phase="reviewing")
        exhausted = self.lapsed_claim(owner_pid=os.getpid() + 1, phase="reviewing")

        for error in (
            f"{CANCELLED_PREFIX}operator",
            f"{PLAN_STALE_PREFIX}base moved",
            "result_error: first",
            "result_error: second",
            "resume_refused: prior session unavailable",
        ):
            TaskAttempt.objects.create(task=coding, ended_at=timezone.now(), exit_code=1, error=error)
        TaskAttempt.objects.create(task=coding, ended_at=timezone.now(), exit_code=0)

        previous = None
        for index in range(5):
            previous = TaskAttempt.objects.create(
                task=reviewing,
                ended_at=timezone.now(),
                exit_code=1,
                error=f"route refusal {index}",
                fallback_from_attempt=previous,
            )
        TaskAttempt.objects.create(
            task=reviewing,
            ended_at=timezone.now(),
            exit_code=0,
            fallback_from_attempt=previous,
        )
        TaskAttempt.objects.create(task=reviewing, ended_at=timezone.now(), exit_code=1, error="api_credit: empty")

        for _ in range(4):
            TaskAttempt.objects.create(task=exhausted, ended_at=timezone.now(), exit_code=0)
        TaskAttempt.objects.create(task=exhausted, ended_at=timezone.now(), exit_code=1, error="api_credit: empty")
        TaskAttempt.objects.create(task=exhausted, ended_at=timezone.now(), exit_code=1, error="review refusal")

        assert Task.objects.reclaim_orphaned_claims() == 2
        for task in (coding, reviewing, exhausted):
            task.refresh_from_db()
        assert coding.status == reviewing.status == Task.Status.PENDING
        assert coding.reclaim_count == reviewing.reclaim_count == 1
        assert exhausted.status == Task.Status.CLAIMED

        assert Task.objects.reap_stale_claims() == 1
        exhausted.refresh_from_db()
        assert exhausted.status == Task.Status.FAILED
        assert (
            TaskAttempt.objects.filter(
                failure_kind=FailureKind.LEASE_EXPIRED, task__in=[coding, reviewing, exhausted]
            ).count()
            == 1
        )

    def test_a_claim_this_process_is_driving_is_not_returned_to_the_queue(self) -> None:
        """The duplicate-execution amplifier: re-queuing here is what a second agent claims."""
        task = self.lapsed_claim(owner_pid=os.getpid())

        with driving(task.pk):
            assert Task.objects.reclaim_orphaned_claims() == 0

        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
        assert task.claimed_by == "headless-worker"
        assert task.owner_pid == os.getpid()

    def test_a_claim_nothing_is_driving_is_still_returned_to_the_queue(self) -> None:
        """A crashed job leaves this worker alive; recovery latency must not regress."""
        task = self.lapsed_claim(owner_pid=os.getpid())

        assert Task.objects.reclaim_orphaned_claims() == 1

        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
        assert task.owner_pid is None
        assert task.owner_pid_namespace == ""

    def test_a_claim_driven_by_another_process_is_still_returned_to_the_queue(self) -> None:
        """No cross-process ``owner_driving_since`` marker: no evidence, lease-only verdict."""
        task = self.lapsed_claim(owner_pid=os.getpid() + 1)

        with driving(task.pk):
            assert Task.objects.reclaim_orphaned_claims() == 1

        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_a_claim_driving_in_the_loops_tick_subprocess_is_not_returned_to_the_queue(self) -> None:
        """#4164 follow-up: reclaim runs in the loops_tick subprocess in production.

        A different interpreter from the one that drives headless work — the in-memory
        ``driving`` registry (exercised above) is ALWAYS empty there. This is the
        regression this fix closes: an ``owner_driving_since`` marker + a provably alive
        owner pid must withhold the reap even with NOTHING entered in-process.
        """
        task = self.lapsed_claim(owner_pid=os.getpid() + 1, driving_since=timezone.now())

        with patch.object(singleton_mod, "pid_alive", lambda _pid: True):
            assert Task.objects.reclaim_orphaned_claims() == 0

        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
        assert task.claimed_by == "headless-worker"

    def test_a_stale_driving_marker_from_a_dead_owner_is_still_returned_to_the_queue(self) -> None:
        """A crash that skips drive_claim's finally leaves a stuck marker.

        The pid check is what still lets a genuinely dead owner's row reclaim normally.
        """
        task = self.lapsed_claim(owner_pid=os.getpid() + 1, driving_since=timezone.now())

        with patch.object(singleton_mod, "pid_alive", lambda _pid: False):
            assert Task.objects.reclaim_orphaned_claims() == 1

        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
        assert task.owner_driving_since is None


class TestReapStaleClaims(LeaseSweepCase):
    def test_a_claim_this_process_is_driving_is_not_failed(self) -> None:
        """Otherwise what ``reclaim_orphaned_claims`` declined to re-queue this fails instead."""
        task = self.lapsed_claim(owner_pid=os.getpid())

        with driving(task.pk):
            assert Task.objects.reap_stale_claims() == 0

        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
        assert not TaskAttempt.objects.filter(task=task).exists()

    def test_a_claim_nothing_is_driving_is_still_failed_with_its_reason(self) -> None:
        task = self.lapsed_claim(owner_pid=os.getpid())

        assert Task.objects.reap_stale_claims() == 1

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert task.owner_pid is None
        assert TaskAttempt.objects.filter(task=task).count() == 1

    def test_a_matching_pid_from_another_namespace_is_still_failed(self) -> None:
        """#4253: an equal integer in a foreign namespace is a collision, not this process."""
        task = self.lapsed_claim(owner_pid=os.getpid(), namespace="pid:[4026999999]")

        with driving(task.pk):
            assert Task.objects.reap_stale_claims() == 1

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_a_claim_driving_in_the_loops_tick_subprocess_is_not_failed(self) -> None:
        """#4164 follow-up: reap_stale_claims runs in the loops_tick subprocess in production.

        The in-memory ``driving`` registry is ALWAYS empty there. The regression this fix
        closes: an ``owner_driving_since`` marker + a provably alive owner pid must
        withhold the reap even with NOTHING entered in-process.
        """
        task = self.lapsed_claim(owner_pid=os.getpid() + 1, driving_since=timezone.now())

        with patch.object(singleton_mod, "pid_alive", lambda _pid: True):
            assert Task.objects.reap_stale_claims() == 0

        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
        assert not TaskAttempt.objects.filter(task=task).exists()

    def test_a_stale_driving_marker_from_a_dead_owner_is_still_failed(self) -> None:
        """A crash that skips drive_claim's finally leaves a stuck marker.

        The pid check is what still lets a genuinely dead owner's row reap normally.
        """
        task = self.lapsed_claim(owner_pid=os.getpid() + 1, driving_since=timezone.now())

        with patch.object(singleton_mod, "pid_alive", lambda _pid: False):
            assert Task.objects.reap_stale_claims() == 1

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert task.owner_driving_since is None


class TestClaimStampsTheOwnerProcess(TestCase):
    """The claim writes are stamped by the real seam, so no namespace is pinned here."""

    def make_task(self) -> Task:
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR)
        return Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket, overlay="test"),
            phase="coding",
        )

    def test_claim_records_this_process(self) -> None:
        task = self.make_task()

        task.claim(claimed_by="headless-worker")

        task.refresh_from_db()
        assert task.owner_pid == os.getpid()
        assert task.owner_pid_namespace == liveness.reader_pid_namespace()

    def test_claim_next_pending_records_this_process(self) -> None:
        self.make_task()

        claimed = Task.objects.claim_next_pending(claimed_by="loop")

        assert claimed is not None
        assert claimed.owner_pid == os.getpid()

    def test_renew_lease_restamps_the_executing_process(self) -> None:
        """The renewing process IS the executor; a dispatcher-claimed row must not keep its pid."""
        task = self.make_task()
        task.claim(claimed_by="headless-worker")
        Task.objects.filter(pk=task.pk).update(owner_pid=1, owner_pid_namespace="pid:[4026999999]")

        task.renew_lease()

        task.refresh_from_db()
        assert task.owner_pid == os.getpid()
        assert task.owner_pid_namespace == liveness.reader_pid_namespace()

    def test_failing_a_task_releases_the_owner_stamp(self) -> None:
        task = self.make_task()
        task.claim(claimed_by="headless-worker")

        task.fail(reason="deliberate", by_holder=True)

        task.refresh_from_db()
        assert task.owner_pid is None
        assert task.owner_pid_namespace == ""
