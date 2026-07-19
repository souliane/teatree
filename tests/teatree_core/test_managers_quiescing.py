"""The drain-then-deploy admission gate on the Task claim path.

``worker_quiescing`` (set by ``t3 worker drain`` for a rolling deploy) must admit
ZERO new work — ``claim_next_pending`` (the headless CAS) and
``_claimable_for_target`` (the interactive/headless claim query) both short-circuit
— WITHOUT touching in-flight work: a CLAIMED task with a live lease keeps renewing
and survives the drain, so a deploy never kills a live sub-agent. The
``active_claims`` SSOT is the predicate the drain waits on.
"""

from datetime import timedelta
from typing import cast

import django.test
from django.utils import timezone

from teatree.core.models import ConfigSetting
from teatree.core.models.task import Task
from tests.factories import TaskFactory


class TestQuiescingBlocksNewClaims(django.test.TestCase):
    def test_claim_next_pending_returns_none_while_quiescing(self) -> None:
        TaskFactory(status=Task.Status.PENDING)
        ConfigSetting.objects.set_value("worker_quiescing", value=True)

        claimed = Task.objects.claim_next_pending(claimed_by="loop")

        assert claimed is None
        # The row is untouched — still PENDING, claimable once the drain lifts.
        assert Task.objects.filter(status=Task.Status.PENDING).count() == 1

    def test_claim_next_pending_admits_again_once_quiescing_clears(self) -> None:
        TaskFactory(status=Task.Status.PENDING)
        ConfigSetting.objects.set_value("worker_quiescing", value=True)
        assert Task.objects.claim_next_pending(claimed_by="loop") is None

        ConfigSetting.objects.set_value("worker_quiescing", value=False)
        claimed = Task.objects.claim_next_pending(claimed_by="loop")

        assert claimed is not None
        assert claimed.status == Task.Status.CLAIMED

    def test_claimable_for_target_is_empty_while_quiescing(self) -> None:
        TaskFactory(status=Task.Status.PENDING, execution_target=Task.ExecutionTarget.INTERACTIVE)
        ConfigSetting.objects.set_value("worker_quiescing", value=True)

        assert not Task.objects.claimable_for_interactive().exists()
        assert not Task.objects.claimable_for_headless().exists()

    def test_default_off_admits_new_work(self) -> None:
        TaskFactory(status=Task.Status.PENDING)
        # No worker_quiescing row: the default is OFF, so claiming proceeds.
        assert Task.objects.claim_next_pending(claimed_by="loop") is not None


class TestQuiescingLeavesInFlightAlone(django.test.TestCase):
    def _claim_with_live_lease(self) -> Task:
        task: Task = cast("Task", TaskFactory(status=Task.Status.PENDING))
        task.claim(claimed_by="loop", lease_seconds=300)  # raises on a lost claim
        task.refresh_from_db()
        return task

    def test_in_flight_lease_renews_and_survives_the_drain(self) -> None:
        task = self._claim_with_live_lease()
        ConfigSetting.objects.set_value("worker_quiescing", value=True)

        # In-flight work renews via renew_lease — a DIFFERENT path from the gated
        # claim — so quiescing never stops it (renew_lease raises if the claim moved).
        task.renew_lease(lease_seconds=300)
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
        assert Task.objects.active_claim_exists() is True

    def test_active_claims_lists_the_in_flight_task(self) -> None:
        task = self._claim_with_live_lease()
        ConfigSetting.objects.set_value("worker_quiescing", value=True)

        assert list(Task.objects.active_claims().values_list("pk", flat=True)) == [task.pk]

    def test_expired_lease_is_not_in_flight(self) -> None:
        task = self._claim_with_live_lease()
        Task.objects.filter(pk=task.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))

        assert Task.objects.active_claim_exists() is False
        assert list(Task.objects.active_claims()) == []
