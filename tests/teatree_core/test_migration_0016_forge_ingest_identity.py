from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

_BEFORE = ("core", "0015_gate_escape")
_AFTER = ("core", "0016_forge_ingest_identity")


class TestForgeIngestIdentityMigration(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._migrate_to_graph_leaves)

    @staticmethod
    def _migrate_to_graph_leaves() -> None:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_existing_settled_forge_rows_lose_payload_but_pending_rows_keep_it(self) -> None:
        MigrationExecutor(connection).migrate([_BEFORE])
        before = MigrationExecutor(connection).loader.project_state([_BEFORE]).apps.get_model("core", "IncomingEvent")
        for source, settled in (("github", True), ("gitlab", True), ("github", False), ("slack", True)):
            before.objects.create(
                source=source,
                idempotency_key=f"{source}:{settled}",
                body="private",
                payload_json={"private": True},
                processed_at=timezone.now() if settled else None,
            )
        before.objects.create(
            source="gitlab",
            idempotency_key="gitlab:dead",
            payload_json={"private": True},
            processed_at=timezone.now(),
            dead_lettered_at=timezone.now(),
        )

        MigrationExecutor(connection).migrate([_AFTER])

        after = MigrationExecutor(connection).loader.project_state([_AFTER]).apps.get_model("core", "IncomingEvent")
        assert list(
            after.objects.filter(
                source__in=["github", "gitlab"], processed_at__isnull=False, dead_lettered_at__isnull=True
            ).values_list("body", "payload_json")
        ) == [("", {}), ("", {})]
        assert after.objects.get(idempotency_key="gitlab:dead").payload_json == {"private": True}
        assert after.objects.get(idempotency_key="github:False").payload_json == {"private": True}
        assert after.objects.get(idempotency_key="slack:True").body == "private"
