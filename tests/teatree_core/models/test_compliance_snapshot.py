"""Instruction-compliance ledger — the persisted root-KPI metric (#2663).

These tests drive the snapshot's rate computation and the per-violation audit
row's remediation transitions directly on the model, so the metric is testable
without the dream pass.
"""

import pytest
from django.test import TestCase

from teatree.core.models import InstructionComplianceRecord, InstructionComplianceSnapshot, RemediationKind, RuleSource


class InstructionComplianceSnapshotTestCase(TestCase):
    """compliance_rate is derived from the observed/violation counts and persisted."""

    def test_rate_is_observed_minus_violations_over_observed(self) -> None:
        snapshot = InstructionComplianceSnapshot.record(instructions_observed=10, violations=2, recurrences_count=1)
        snapshot.refresh_from_db()
        assert snapshot.compliance_rate == pytest.approx(0.8)
        assert snapshot.violations == 2
        assert snapshot.recurrences_count == 1

    def test_rate_is_one_when_nothing_observed(self) -> None:
        # A pass that observed no instructions is not a violation — rate is a clean 1.0.
        snapshot = InstructionComplianceSnapshot.record(instructions_observed=0, violations=0, recurrences_count=0)
        assert snapshot.compliance_rate == pytest.approx(1.0)

    def test_latest_returns_the_most_recent_snapshot(self) -> None:
        InstructionComplianceSnapshot.record(instructions_observed=4, violations=0, recurrences_count=0)
        newest = InstructionComplianceSnapshot.record(instructions_observed=4, violations=1, recurrences_count=0)
        assert InstructionComplianceSnapshot.objects.latest_for() == newest


class InstructionComplianceRecordTestCase(TestCase):
    """A per-violation audit row carries its source, identity, recurrence flag, remediation."""

    def test_recurrence_escalation_records_the_ticket_url(self) -> None:
        record = InstructionComplianceRecord.objects.create(
            rule_source=RuleSource.MEMORY,
            rule_identity="feedback_askuserquestion_gate",
            evidence="AskUserQuestion fired despite the memory",
            is_recurrence=True,
        )
        record.mark_escalated("https://github.com/souliane/teatree/issues/9100")
        record.refresh_from_db()
        assert record.remediation == RemediationKind.ESCALATION
        assert record.escalation_url == "https://github.com/souliane/teatree/issues/9100"

    def test_open_escalations_lists_escalated_recurrences(self) -> None:
        escalated = InstructionComplianceRecord.objects.create(
            rule_source=RuleSource.MEMORY,
            rule_identity="feedback_comment_bloat",
            is_recurrence=True,
        )
        escalated.mark_escalated("https://github.com/souliane/teatree/issues/9200")
        InstructionComplianceRecord.objects.create(
            rule_source=RuleSource.SKILL,
            rule_identity="ship-one-open-pr",
            is_recurrence=False,
        )
        listed = list(InstructionComplianceRecord.objects.open_escalations())
        assert listed == [escalated]
