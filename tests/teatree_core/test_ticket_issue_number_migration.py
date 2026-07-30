"""The ``0004`` backfill populates ``issue_number`` on pre-existing rows.

A live dogfooded install already carries tickets with no ``issue_number`` column.
The forward migration adds the indexed column AND backfills it from ``issue_url``
so ``_ticket_by_number`` resolves those rows on day one — without waiting for each
to be re-saved. Anti-vacuous: dropping the ``RunPython`` leaves the row on the
``""`` AddField default and this goes RED.
"""

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

_BEFORE = ("core", "0003_implementedissuemarker_claim_ref_sha")
_AFTER = ("core", "0004_ticket_issue_number")
_ISSUE_URL = "https://github.com/example/repo/issues/466"
_NO_NUMBER_URL = "https://example.com/no-number"


@pytest.mark.timeout(240)
class TestIssueNumberBackfill(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    def test_backfill_derives_issue_number_from_issue_url(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        old_apps = executor.loader.project_state(_BEFORE).apps
        old_ticket = old_apps.get_model("core", "Ticket")
        # The 0003 schema has no issue_number column; the historical model's base
        # save cannot set it, so the row lands column-less — exactly the fleet state.
        numbered = old_ticket.objects.create(overlay="", issue_url=_ISSUE_URL)
        blank = old_ticket.objects.create(overlay="", issue_url=_NO_NUMBER_URL)

        executor = MigrationExecutor(connection)
        executor.migrate([_AFTER])
        new_apps = executor.loader.project_state(_AFTER).apps
        new_ticket = new_apps.get_model("core", "Ticket")

        assert new_ticket.objects.get(pk=numbered.pk).issue_number == "466"
        assert new_ticket.objects.get(pk=blank.pk).issue_number == ""
