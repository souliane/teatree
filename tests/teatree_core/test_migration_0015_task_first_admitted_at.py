from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

_BEFORE = ("core", "0014_review_verdict_identity_and_verdict_key")
_AFTER = ("core", "0015_task_first_admitted_at")


@pytest.mark.timeout(480)
class TestFirstAdmissionBackfill(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._migrate_to_graph_leaves)

    @staticmethod
    def _migrate_to_graph_leaves() -> None:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_existing_admission_is_retained_and_unadmitted_task_stays_null(self) -> None:
        MigrationExecutor(connection).migrate([_BEFORE])
        prior = MigrationExecutor(connection).loader.project_state([_BEFORE]).apps
        ticket = prior.get_model("core", "Ticket").objects.create()
        session = prior.get_model("core", "Session").objects.create(ticket=ticket)
        tasks = prior.get_model("core", "Task").objects
        started = timezone.now() - timedelta(hours=8)
        admitted = tasks.create(ticket=ticket, session=session, phase="coding", admitted_at=started)
        unadmitted = tasks.create(ticket=ticket, session=session, phase="coding")

        MigrationExecutor(connection).migrate([_AFTER])
        current = MigrationExecutor(connection).loader.project_state([_AFTER]).apps.get_model("core", "Task")

        assert current.objects.get(pk=admitted.pk).first_admitted_at == started
        assert current.objects.get(pk=unadmitted.pk).first_admitted_at is None
