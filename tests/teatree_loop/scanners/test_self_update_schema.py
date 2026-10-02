"""The post-pull schema reconcile — the step that sequences code and schema (#3901).

`self_update` fast-forwards a live worker's clone; applying the schema was a
separate boot-time step nothing ordered against it. This is the missing sequencer:
the moment a clone advances, the reconcile either brings the control DB up to the
code's migration graph or shouts. The one outcome it removes is silently continuing.

The migrate and the DM are the two unstoppable externals here (an in-process
`migrate` against the shared test DB, and Slack), so those are stubbed; the
reconcile's own decision ladder, its cache invalidation and its LOUD path are real.
"""

from unittest.mock import patch

import django.test

from teatree.core.gates.schema_guard import SelfDbMigrationError
from teatree.core.schema_readiness import cached_schema_readiness, invalidate_schema_readiness
from teatree.loop.scanners.self_update_schema import SchemaReconcileState, reconcile_schema_after_pull

_PROBE = "teatree.core.schema_readiness.pending_migrations"
_MIGRATE = "teatree.loop.scanners.self_update_schema.migrate_self_db"
_NOTIFY = "teatree.loop.scanners.self_update_schema.notify_user"
_PENDING = ["core.0042_widget", "core.0043_gizmo"]


class _ReconcileBase(django.test.TestCase):
    def setUp(self) -> None:
        super().setUp()
        invalidate_schema_readiness()
        self.addCleanup(invalidate_schema_readiness)


class TestSchemaAlreadyCurrent(_ReconcileBase):
    def test_current_schema_is_a_no_op(self) -> None:
        with (
            patch(_PROBE, return_value=[]),
            patch(_MIGRATE) as migrate,
            patch(_NOTIFY) as notify,
        ):
            result = reconcile_schema_after_pull(label="teatree", head_sha="a" * 40)

        assert result.state is SchemaReconcileState.CURRENT
        assert result.applied == ()
        migrate.assert_not_called()
        notify.assert_not_called()


class TestSchemaBehindIsApplied(_ReconcileBase):
    def test_pending_migrations_are_applied_in_place(self) -> None:
        with (
            patch(_PROBE, return_value=_PENDING),
            patch(_MIGRATE, return_value=_PENDING) as migrate,
            patch(_NOTIFY) as notify,
        ):
            result = reconcile_schema_after_pull(label="teatree", head_sha="b" * 40)

        assert result.state is SchemaReconcileState.MIGRATED
        assert result.applied == tuple(_PENDING)
        migrate.assert_called_once()
        notify.assert_not_called()

    def test_a_stale_current_verdict_cannot_survive_the_pull(self) -> None:
        """The memo is void the moment the code on disk moves — that is the whole bug."""
        with patch(_PROBE, return_value=[]):
            assert cached_schema_readiness().admits_work is True

        with (
            patch(_PROBE, return_value=_PENDING),
            patch(_MIGRATE, return_value=_PENDING),
            patch(_NOTIFY),
        ):
            result = reconcile_schema_after_pull(label="teatree", head_sha="c" * 40)

        assert result.state is SchemaReconcileState.MIGRATED

    def test_the_next_claim_re_probes_after_a_successful_migrate(self) -> None:
        with (
            patch(_PROBE, return_value=_PENDING) as probe,
            patch(_MIGRATE, return_value=_PENDING),
            patch(_NOTIFY),
        ):
            reconcile_schema_after_pull(label="teatree", head_sha="d" * 40)
            probe.reset_mock()
            probe.return_value = []

            assert cached_schema_readiness().admits_work is True

        assert probe.call_count == 1


class TestFailedMigrateIsLoud(_ReconcileBase):
    def test_a_failing_migrate_pages_the_owner_and_reports_failed(self) -> None:
        with (
            patch(_PROBE, return_value=_PENDING),
            patch(_MIGRATE, side_effect=SelfDbMigrationError("disk full")),
            patch(_NOTIFY) as notify,
        ):
            result = reconcile_schema_after_pull(label="teatree", head_sha="e" * 40)

        assert result.state is SchemaReconcileState.FAILED
        assert "disk full" in result.detail
        body = notify.call_args.args[0]
        assert "core.0042_widget" in body
        assert "teatree" in body

    def test_an_unverifiable_schema_pages_rather_than_assuming_current(self) -> None:
        with (
            patch(_PROBE, side_effect=RuntimeError("graph unreadable")),
            patch(_MIGRATE) as migrate,
            patch(_NOTIFY) as notify,
        ):
            result = reconcile_schema_after_pull(label="teatree", head_sha="f" * 40)

        assert result.state is SchemaReconcileState.FAILED
        migrate.assert_not_called()
        notify.assert_called_once()

    def test_the_notice_is_keyed_per_clone_and_head_so_it_pages_once(self) -> None:
        with (
            patch(_PROBE, return_value=_PENDING),
            patch(_MIGRATE, side_effect=SelfDbMigrationError("disk full")),
            patch(_NOTIFY) as notify,
        ):
            reconcile_schema_after_pull(label="teatree", head_sha="1" * 40)
            reconcile_schema_after_pull(label="teatree", head_sha="2" * 40)

        keys = [call.kwargs["idempotency_key"] for call in notify.call_args_list]
        assert keys[0] != keys[1]
        assert all(key.startswith("schema_behind_code:teatree:") for key in keys)

    def test_a_dead_notify_transport_never_breaks_the_tick(self) -> None:
        with (
            patch(_PROBE, return_value=_PENDING),
            patch(_MIGRATE, side_effect=SelfDbMigrationError("disk full")),
            patch(_NOTIFY, side_effect=RuntimeError("slack down")),
        ):
            result = reconcile_schema_after_pull(label="teatree", head_sha="9" * 40)

        assert result.state is SchemaReconcileState.FAILED

    def test_a_failed_reconcile_leaves_the_claim_gate_refusing(self) -> None:
        """The worker must not resume: no migrate landed, so the DB is still behind."""
        with (
            patch(_PROBE, return_value=_PENDING),
            patch(_MIGRATE, side_effect=SelfDbMigrationError("disk full")),
            patch(_NOTIFY),
        ):
            reconcile_schema_after_pull(label="teatree", head_sha="8" * 40)

            assert cached_schema_readiness().admits_work is False
