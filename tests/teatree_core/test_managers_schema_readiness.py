"""The schema-readiness admission gate on the Task claim path (#3901).

`self_update` advances a live worker's CODE on a cadence while the schema migrate
is a separate boot-time step, so a worker can hold code whose models the control
DB does not carry. While that is true the claim path must admit ZERO new work —
`claim_next_pending` (the headless CAS) and `_claimable_for_target` (the
interactive/headless claim query) both short-circuit, exactly as they do for
`worker_quiescing` — rather than dispatch an agent that will crash on the first
missing relation.

These drive the REAL gate: the pending-migration probe is the only thing stubbed,
so the verdict, the memo, the kill switch and both claim chokepoints are exercised
end to end. Every case asserts the row survives untouched — a refused claim is a
deferral, never a loss.
"""

from contextlib import AbstractContextManager
from typing import cast
from unittest.mock import patch

import django.test

from teatree.core.models import ConfigSetting
from teatree.core.models.task import Task
from teatree.core.schema_readiness import invalidate_schema_readiness
from tests.factories import TaskFactory

_PROBE = "teatree.core.schema_readiness.pending_migrations"
_BEHIND = ["core.0042_widget", "core.0043_gizmo"]


class _SchemaGateBase(django.test.TestCase):
    def setUp(self) -> None:
        super().setUp()
        invalidate_schema_readiness()
        self.addCleanup(invalidate_schema_readiness)

    def _behind(self) -> AbstractContextManager[object]:
        return patch(_PROBE, return_value=_BEHIND)


class TestSchemaBehindBlocksNewClaims(_SchemaGateBase):
    def test_claim_next_pending_returns_none_while_the_schema_is_behind(self) -> None:
        TaskFactory(status=Task.Status.PENDING)

        with self._behind():
            claimed = Task.objects.claim_next_pending(claimed_by="loop")

        assert claimed is None
        assert Task.objects.filter(status=Task.Status.PENDING).count() == 1

    def test_claimable_for_target_is_empty_while_the_schema_is_behind(self) -> None:
        TaskFactory(status=Task.Status.PENDING, execution_target=Task.ExecutionTarget.INTERACTIVE)

        with self._behind():
            assert not Task.objects.claimable_for_interactive().exists()
            assert not Task.objects.claimable_for_headless().exists()

    def test_claim_admits_again_once_the_migrations_are_applied(self) -> None:
        TaskFactory(status=Task.Status.PENDING)
        with self._behind():
            assert Task.objects.claim_next_pending(claimed_by="loop") is None

        invalidate_schema_readiness()
        with patch(_PROBE, return_value=[]):
            claimed = Task.objects.claim_next_pending(claimed_by="loop")

        assert claimed is not None
        assert claimed.status == Task.Status.CLAIMED

    def test_current_schema_admits_new_work(self) -> None:
        TaskFactory(status=Task.Status.PENDING)

        with patch(_PROBE, return_value=[]):
            assert Task.objects.claim_next_pending(claimed_by="loop") is not None


class TestUnverifiableSchemaFailsClosed(_SchemaGateBase):
    def test_a_probe_that_raised_refuses_the_claim(self) -> None:
        """UNKNOWN is not CURRENT — a gate that cannot tell must not admit."""
        TaskFactory(status=Task.Status.PENDING)

        with patch(_PROBE, side_effect=RuntimeError("migration graph unreadable")):
            claimed = Task.objects.claim_next_pending(claimed_by="loop")

        assert claimed is None
        assert Task.objects.filter(status=Task.Status.PENDING).count() == 1


class TestKillSwitchIsTheNeverLockoutEscape(_SchemaGateBase):
    def test_disabled_gate_admits_work_against_a_behind_schema(self) -> None:
        TaskFactory(status=Task.Status.PENDING)
        ConfigSetting.objects.set_value("schema_readiness_gate_enabled", value=False)

        with self._behind():
            claimed = Task.objects.claim_next_pending(claimed_by="loop")

        assert claimed is not None

    def test_default_is_enabled(self) -> None:
        """No stored row: the guarantee is on, so a behind schema still refuses."""
        TaskFactory(status=Task.Status.PENDING)

        with self._behind():
            assert Task.objects.claim_next_pending(claimed_by="loop") is None


class TestSchemaGateLeavesInFlightAlone(_SchemaGateBase):
    def test_in_flight_lease_renews_while_the_schema_is_behind(self) -> None:
        """The gate defers NEW work only — it never kills a live sub-agent mid-task."""
        task = cast("Task", TaskFactory(status=Task.Status.PENDING))
        task.claim(claimed_by="loop", lease_seconds=300)

        with self._behind():
            task.renew_lease(lease_seconds=300)

        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
