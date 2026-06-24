"""Dream phase 3c — the instruction-compliance accountant (#2663).

The detector mines one pass's extract + memory corpus for instruction-compliance
failures (a rule was PRESENT/AVAILABLE and the agent acted against it) and the
escalation rule turns each recurrence into ONE deduped enforcement ticket — never
another memory. These tests drive both with a fake code host and synthetic
transcripts so the whole phase runs without an LLM or a live forge.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from django.test import TestCase

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import InstructionComplianceRecord, RemediationKind, RuleSource
from teatree.loops.dream.compliance import (
    ComplianceFinding,
    build_compliance_snapshot,
    detect_compliance_failures,
    escalate_recurrences,
    persist_compliance_pass,
)
from teatree.loops.dream.engine import ConsolidationExtract, WeightedSnippet


def _memory_snippet(name: str, body: str) -> WeightedSnippet:
    return WeightedSnippet(path=Path(f"/memory/{name}"), kind="memory", weight=90, text=body)


def _transcript_snippet(name: str, body: str) -> WeightedSnippet:
    return WeightedSnippet(path=Path(f"/sessions/{name}"), kind="main", weight=100, text=body)


def _extract(*snippets: WeightedSnippet) -> ConsolidationExtract:
    return ConsolidationExtract(snippets=tuple(snippets), truncated=False)


#: A memory-backed rule (a feedback_ slug) whose subject recurs in a fresh
#: user-correction turn — the recurrence the detector must flag.
_MEMORY_BODY = (
    "name: feedback_askuserquestion_overuse\n"
    "The AskUserQuestion gate must not fire for routine obstacles — make a "
    "reasonable guess and keep working.\n"
)
_VIOLATION_TURN = (
    '{"type": "user", "content": "I told you again — stop firing AskUserQuestion '
    'for routine obstacles, you do not follow instructions!!"}'
)
_CLEAN_TURN = '{"type": "assistant", "content": "Implemented the feature and ran the tests."}'


def _fake_host(*, created_url: str = "https://github.com/souliane/teatree/issues/9300") -> CodeHostBackend:
    host = MagicMock(spec=CodeHostBackend)
    host.search_open_issues.return_value = []
    host.create_issue.return_value = {"html_url": created_url}
    return host


class DetectComplianceFailuresTestCase(TestCase):
    """A memory-backed rule violated in a fresh correction turn is a recurrence."""

    def test_memory_backed_rule_violated_again_is_a_recurrence(self) -> None:
        extract = _extract(
            _memory_snippet("feedback_askuserquestion_overuse.md", _MEMORY_BODY),
            _transcript_snippet("session-a.jsonl", _VIOLATION_TURN),
        )
        findings = detect_compliance_failures(extract)
        recurrences = [f for f in findings if f.is_recurrence]
        assert len(recurrences) == 1
        finding = recurrences[0]
        assert finding.rule_source is RuleSource.MEMORY
        assert finding.rule_identity == "feedback_askuserquestion_overuse"
        assert "AskUserQuestion" in finding.evidence

    def test_clean_transcript_yields_no_false_positive(self) -> None:
        extract = _extract(
            _memory_snippet("feedback_askuserquestion_overuse.md", _MEMORY_BODY),
            _transcript_snippet("session-clean.jsonl", _CLEAN_TURN),
        )
        findings = detect_compliance_failures(extract)
        assert findings == []

    def test_violation_without_a_backing_memory_is_not_a_recurrence(self) -> None:
        # A correction whose rule has no durable memory is still a compliance
        # failure, but a FIRST occurrence — not a recurrence (no escalation yet).
        directive = (
            '{"type": "user", "content": "do not rename the public API again — you keep breaking the contract!!"}'
        )
        extract = _extract(_transcript_snippet("session-b.jsonl", directive))
        findings = detect_compliance_failures(extract)
        assert findings
        assert all(not f.is_recurrence for f in findings)
        assert all(f.rule_source is RuleSource.IN_SESSION for f in findings)


class EscalateRecurrencesTestCase(TestCase):
    """A recurrence files exactly one deduped enforcement ticket, never a memory."""

    def _recurrence(self, identity: str = "feedback_askuserquestion_overuse") -> ComplianceFinding:
        return ComplianceFinding(
            rule_source=RuleSource.MEMORY,
            rule_identity=identity,
            evidence="AskUserQuestion fired again despite the memory",
            is_recurrence=True,
        )

    def test_one_recurrence_files_one_gate_or_eval_ticket(self) -> None:
        host = _fake_host()
        outcomes = escalate_recurrences([self._recurrence()], host, repo="souliane/teatree")
        assert len(outcomes) == 1
        assert outcomes[0].filed is True
        host.create_issue.assert_called_once()
        _, kwargs = host.create_issue.call_args
        # The ticket must prescribe a STRUCTURAL fix (a gate or an eval), never "write a memory".
        body = kwargs["body"].lower()
        assert "gate" in body or "eval" in body
        assert "write another memory" not in body
        assert "needs-triage" in kwargs["labels"]

    def test_two_recurrences_of_the_same_rule_file_one_ticket(self) -> None:
        host = _fake_host()
        outcomes = escalate_recurrences([self._recurrence(), self._recurrence()], host, repo="souliane/teatree")
        filed = [o for o in outcomes if o.filed]
        assert len(filed) == 1
        host.create_issue.assert_called_once()

    def test_existing_open_escalation_is_reused_not_duplicated(self) -> None:
        host = MagicMock(spec=CodeHostBackend)
        host.search_open_issues.return_value = [
            {
                "html_url": "https://github.com/souliane/teatree/issues/55",
                "body": "<!-- compliance-recurrence feedback_askuserquestion_overuse -->",
            }
        ]
        outcomes = escalate_recurrences([self._recurrence()], host, repo="souliane/teatree")
        assert outcomes[0].filed is False
        assert outcomes[0].ticket_url == "https://github.com/souliane/teatree/issues/55"
        host.create_issue.assert_not_called()

    def test_non_recurrence_findings_are_never_escalated(self) -> None:
        first_occurrence = ComplianceFinding(
            rule_source=RuleSource.IN_SESSION,
            rule_identity="rename-public-api",
            evidence="renamed the API again",
            is_recurrence=False,
        )
        host = _fake_host()
        outcomes = escalate_recurrences([first_occurrence], host, repo="souliane/teatree")
        assert outcomes == []
        host.create_issue.assert_not_called()


class PersistCompliancePassTestCase(TestCase):
    """A pass persists one snapshot plus one audit row per finding."""

    def test_snapshot_and_records_are_persisted_with_the_rate(self) -> None:
        findings = [
            ComplianceFinding(
                rule_source=RuleSource.MEMORY,
                rule_identity="feedback_a",
                evidence="violated a",
                is_recurrence=True,
            ),
            ComplianceFinding(
                rule_source=RuleSource.IN_SESSION,
                rule_identity="directive-b",
                evidence="violated b",
                is_recurrence=False,
            ),
        ]
        snapshot = persist_compliance_pass(findings, instructions_observed=10)
        assert snapshot.violations == 2
        assert snapshot.recurrences_count == 1
        assert snapshot.compliance_rate == pytest.approx(0.8)
        records = list(InstructionComplianceRecord.objects.filter(snapshot=snapshot))
        assert len(records) == 2
        recurrence_row = next(r for r in records if r.is_recurrence)
        assert recurrence_row.rule_source == RuleSource.MEMORY
        assert recurrence_row.remediation == RemediationKind.NONE

    def test_escalated_recurrence_record_carries_the_escalation_url(self) -> None:
        host = _fake_host()
        finding = ComplianceFinding(
            rule_source=RuleSource.MEMORY,
            rule_identity="feedback_a",
            evidence="violated a",
            is_recurrence=True,
        )
        snapshot = persist_compliance_pass([finding], instructions_observed=4)
        escalate_recurrences([finding], host, repo="souliane/teatree", snapshot=snapshot)
        row = InstructionComplianceRecord.objects.get(snapshot=snapshot, rule_identity="feedback_a")
        assert row.remediation == RemediationKind.ESCALATION
        assert row.escalation_url == "https://github.com/souliane/teatree/issues/9300"


class BuildComplianceSnapshotTestCase(TestCase):
    """The detect→snapshot helper counts observed instructions across the corpus."""

    def test_observed_count_includes_memory_and_directive_rules(self) -> None:
        extract = _extract(
            _memory_snippet("feedback_askuserquestion_overuse.md", _MEMORY_BODY),
            _transcript_snippet("session-a.jsonl", _VIOLATION_TURN),
        )
        result = build_compliance_snapshot(extract)
        # At least the one memory rule is an observed instruction; the violation is counted.
        assert result.instructions_observed >= 1
        assert result.violations >= 1
        assert any(f.is_recurrence for f in result.findings)
