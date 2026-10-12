"""An INSERT that omits ``claim_lineage`` stores ``""``: a process running the model before the column keeps working."""

import django.test

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
