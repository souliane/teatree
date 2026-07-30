"""The ``0026`` data migration re-homes mislabeled reviewer DELIVERED tickets.

Reviewer-role tickets used to short-circuit to DELIVERED, so the board showed
them as author-merged "Landed" work. DELIVERED now means only author work merged
to main; the migration moves the reviewer ghosts to REVIEW_POSTED and leaves a
genuinely-merged author DELIVERED ticket alone. Anti-vacuous: dropping the
``RunPython`` leaves the reviewer ticket on ``delivered`` and this goes RED.
"""

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

_BEFORE = ("core", "0025_alter_ticket_state")
_AFTER = ("core", "0026_rehome_reviewer_delivered_tickets")


@pytest.mark.timeout(240)
class TestRehomeReviewerDelivered(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    def test_reviewer_ghost_moves_and_merged_author_stays(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        old_apps = executor.loader.project_state(_BEFORE).apps
        old_ticket = old_apps.get_model("core", "Ticket")
        old_clear = old_apps.get_model("core", "MergeClear")
        old_audit = old_apps.get_model("core", "MergeAudit")

        reviewer_ghost = old_ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://example.com/pr/1",
            role="reviewer",
            state="delivered",
        )
        # An author ticket genuinely merged to main: a MergeClear + MergeAudit
        # exists, so it must stay DELIVERED.
        merged_author = old_ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://example.com/issues/2",
            role="author",
            state="delivered",
        )
        clear = old_clear.objects.create(
            slug="t3-teatree",
            pr_id=2,
            ticket=merged_author,
            reviewed_sha="a" * 40,
            reviewer_identity="reviewer",
            gh_verify_result="green",
            blast_class="logic",
        )
        old_audit.objects.create(clear=clear, merged_sha="b" * 40, required_checks_status="green")
        # A reviewer ticket that DID produce a merge audit is real merged work,
        # so it must also stay DELIVERED.
        reviewer_merged = old_ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://example.com/pr/3",
            role="reviewer",
            state="delivered",
        )
        reviewer_clear = old_clear.objects.create(
            slug="t3-teatree",
            pr_id=3,
            ticket=reviewer_merged,
            reviewed_sha="c" * 40,
            reviewer_identity="reviewer",
            gh_verify_result="green",
            blast_class="logic",
        )
        old_audit.objects.create(clear=reviewer_clear, merged_sha="d" * 40, required_checks_status="green")

        executor = MigrationExecutor(connection)
        executor.migrate([_AFTER])
        new_apps = executor.loader.project_state(_AFTER).apps
        new_ticket = new_apps.get_model("core", "Ticket")

        assert new_ticket.objects.get(pk=reviewer_ghost.pk).state == "review_posted"
        assert new_ticket.objects.get(pk=merged_author.pk).state == "delivered"
        assert new_ticket.objects.get(pk=reviewer_merged.pk).state == "delivered"
