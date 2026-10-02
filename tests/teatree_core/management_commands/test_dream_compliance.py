"""``manage.py dream compliance`` — print the latest compliance snapshot (#2663).

The subcommand reads the persisted :class:`InstructionComplianceSnapshot` (rate +
recurrence count) and the open escalations, so the operator can see the root-KPI
trend without opening the DB. These tests drive it through ``call_command`` with a
captured stdout.
"""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import InstructionComplianceRecord, InstructionComplianceSnapshot, RuleSource


class DreamComplianceShowTestCase(TestCase):
    """`dream compliance` prints the latest snapshot's rate, recurrences, escalations."""

    def test_prints_latest_rate_and_recurrence_count(self) -> None:
        InstructionComplianceSnapshot.record(instructions_observed=10, violations=2, recurrences_count=1)
        out = StringIO()
        call_command("dream", "compliance", stdout=out)
        printed = out.getvalue()
        assert "0.80" in printed
        assert "1 recurrence" in printed

    def test_lists_open_escalations(self) -> None:
        InstructionComplianceSnapshot.record(instructions_observed=4, violations=1, recurrences_count=1)
        escalated = InstructionComplianceRecord.objects.create(
            rule_source=RuleSource.MEMORY,
            rule_identity="feedback_askuserquestion_overuse",
            is_recurrence=True,
        )
        escalated.mark_escalated("https://github.com/souliane/teatree/issues/9100")
        out = StringIO()
        call_command("dream", "compliance", stdout=out)
        printed = out.getvalue()
        assert "feedback_askuserquestion_overuse" in printed
        assert "https://github.com/souliane/teatree/issues/9100" in printed

    def test_reports_no_snapshot_when_none_recorded(self) -> None:
        out = StringIO()
        call_command("dream", "compliance", stdout=out)
        assert "no compliance snapshot" in out.getvalue().lower()
