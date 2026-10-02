"""The ``0088`` data migration unflags recurrences a brief minted, and only those.

Anti-vacuous in both directions: dropping the ``RunPython`` leaves the brief-minted
rows flagged and the first three assertions go RED; widening the predicate to cover
short turns would unflag the genuine correction the last assertion pins.
"""

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

_BEFORE = ("core", "0087_review_head_refresh")
_AFTER = ("core", "0088_unflag_misattributed_compliance_recurrences")


@pytest.mark.timeout(240)
class TestUnflagMisattributedRecurrences(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    def test_brief_minted_rows_unflag_and_a_real_correction_survives(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        old_apps = executor.loader.project_state(_BEFORE).apps
        record = old_apps.get_model("core", "InstructionComplianceRecord")

        capped = record.objects.create(
            rule_source="memory",
            rule_identity="do-not-hand-run-the-loops",
            # Exactly the cap INCLUDING the role header — the real shape of a stored
            # truncation. A fixture that caps the body instead hides the off-by-header.
            evidence=('{"role": "user"} ' + "x" * 500)[:500],
            is_recurrence=True,
            remediation="escalation",
            escalation_url="https://example.com/issues/1",
        )
        opener = record.objects.create(
            rule_source="memory",
            rule_identity="a-typed-feedback-rule",
            evidence='{"role": "user"} Work on ticket 9001. Current phase: coding Reason: it is green.',
            is_recurrence=True,
            remediation="escalation",
            escalation_url="https://example.com/issues/2",
        )
        index_file = record.objects.create(
            rule_source="memory",
            rule_identity="MEMORY_ARCHIVE",
            evidence='{"role": "user"} Repo: a project. Question, very thorough search.',
            is_recurrence=True,
            remediation="escalation",
            escalation_url="https://example.com/issues/3",
        )
        genuine = record.objects.create(
            rule_source="memory",
            rule_identity="no-unsolicited-progress-dms",
            evidence='{"role": "user"} why are you spamming me on slack? stop it',
            is_recurrence=True,
            remediation="escalation",
            escalation_url="https://example.com/issues/4",
        )
        first_occurrence = record.objects.create(
            rule_source="in_session",
            rule_identity="some-directive",
            evidence=('{"role": "user"} ' + "y" * 500)[:500],
            is_recurrence=False,
        )

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([_AFTER])
        new_apps = executor.loader.project_state(_AFTER).apps
        after = new_apps.get_model("core", "InstructionComplianceRecord")

        for row in (capped, opener, index_file):
            refreshed = after.objects.get(pk=row.pk)
            assert refreshed.is_recurrence is False, f"{row.rule_identity} must be unflagged"
            assert refreshed.remediation == "none"
            assert refreshed.escalation_url == ""

        survivor = after.objects.get(pk=genuine.pk)
        assert survivor.is_recurrence is True, "a short human correction is a real recurrence"
        assert survivor.escalation_url == "https://example.com/issues/4"

        untouched = after.objects.get(pk=first_occurrence.pk)
        assert untouched.is_recurrence is False
        assert untouched.remediation == "none"
