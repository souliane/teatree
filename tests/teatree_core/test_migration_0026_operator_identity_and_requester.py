from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

_BEFORE = ("core", "0025_ticketdmthread_ticketdmpost")
_AFTER = ("core", "0026_operator_identity_and_requester")


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
        assert after.objects.get(pk=ticket.pk).requested_by == ""
