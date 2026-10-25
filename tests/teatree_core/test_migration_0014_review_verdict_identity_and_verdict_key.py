import pytest
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

_BEFORE = ("core", "0013_resume_refused")
_VERDICT_KEY = ("core", "0014_review_verdict_identity_and_verdict_key")


@pytest.mark.timeout(480)
class TestReviewVerdictKeyRollback(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._migrate_to_graph_leaves)

    @staticmethod
    def _migrate_to_graph_leaves() -> None:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_empty_database_can_reverse_the_constraint(self) -> None:
        MigrationExecutor(connection).migrate([_BEFORE])

        assert _VERDICT_KEY not in MigrationExecutor(connection).loader.applied_migrations

    def test_rollback_refuses_to_collapse_a_hold_and_pass(self) -> None:
        verdicts = (
            MigrationExecutor(connection)
            .loader.project_state([_VERDICT_KEY])
            .apps.get_model("core", "ReviewVerdict")
            .objects
        )
        for verdict in ("hold", "merge_safe"):
            verdicts.create(
                slug="souliane/teatree",
                pr_id=4950,
                reviewed_sha="a" * 40,
                reviewer_identity="cold-reviewer",
                reviewer_identity_normalized="cold-reviewer",
                verdict=verdict,
                blast_class="logic",
                gh_verify_result="green",
            )

        with pytest.raises(IrreversibleError, match="would discard distinct verdicts"):
            MigrationExecutor(connection).migrate([_BEFORE])

        assert verdicts.filter(pr_id=4950).count() == 2
        assert _VERDICT_KEY in MigrationExecutor(connection).loader.applied_migrations
