from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from teatree.core.notify_targets import addressee_for

_BEFORE = ("core", "0031_review_hold_disposition_and_merge_attribution")
_AFTER = ("core", "0032_operator_identity_and_requester")


class OperatorRequesterMigrationTestCase(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._migrate_to_graph_leaves)

    @staticmethod
    def _migrate_to_graph_leaves() -> None:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_existing_tickets_keep_an_unknown_requester(self) -> None:
        MigrationExecutor(connection).migrate([_BEFORE])
        before = MigrationExecutor(connection).loader.project_state([_BEFORE]).apps.get_model("core", "Ticket")
        ticket = before.objects.create(overlay="teatree")

        MigrationExecutor(connection).migrate([_AFTER])

        after = MigrationExecutor(connection).loader.project_state([_AFTER]).apps.get_model("core", "Ticket")
        migrated = after.objects.get(pk=ticket.pk)
        assert migrated.requested_by == ""
        assert addressee_for(migrated, owner_id="U-OWNER").user_id == "U-OWNER"
