"""An INSERT that omits ``claim_lineage`` stores ``""``: a process running the model before the column keeps working."""

import django.test
from django.db.migrations.loader import MigrationLoader

from teatree.core.models import ImplementedIssueMarker
from tests.factories import ImplementedIssueMarkerFactory
from tests.teatree_core._row_clone import clone_row_omitting


class TestClaimLineageHasADatabaseDefault(django.test.TestCase):
    def test_an_insert_that_omits_the_column_stores_blank(self) -> None:
        marker = ImplementedIssueMarkerFactory(
            issue_url="https://github.com/acme/widget/issues/1", claim_lineage="0" * 32
        )

        clone_row_omitting(
            ImplementedIssueMarker._meta.db_table,
            "claim_lineage",
            marker.pk,
            replacing={"issue_url": "https://github.com/acme/widget/issues/2"},
        )

        clone = ImplementedIssueMarker.objects.exclude(pk=marker.pk).get()
        assert clone.claim_lineage == ""

    def test_the_model_declares_the_database_default_its_migration_installs(self) -> None:
        migrated = MigrationLoader(None).project_state().models["core", "implementedissuemarker"]

        declared = ImplementedIssueMarker._meta.get_field("claim_lineage").db_default

        assert declared == migrated.fields["claim_lineage"].db_default == ""
