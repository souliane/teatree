"""Dream phase 3c — the instruction-compliance accountant (#2663).

The detector mines one pass's extract + memory corpus for instruction-compliance
failures (a rule was PRESENT/AVAILABLE and the agent acted against it) and the
escalation rule turns each recurrence into ONE deduped enforcement ticket — never
another memory. These tests drive both with a fake code host and synthetic
transcripts so the whole phase runs without an LLM or a live forge.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.db.utils import OperationalError
from django.test import TestCase

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import InstructionComplianceRecord, InstructionComplianceSnapshot, RemediationKind, RuleSource
from teatree.core.models.ticket import Ticket
from teatree.loops.dream import batch_promote as bp_module
from teatree.loops.dream.batch_promote import PromotionBatch
from teatree.loops.dream.compliance import (
    ComplianceFinding,
    build_compliance_snapshot,
    detect_compliance_failures,
    escalate_recurrences,
    persist_compliance_pass,
    run_compliance_escalation,
    run_compliance_measurement,
)
from teatree.loops.dream.replay import ConsolidationExtract, WeightedSnippet
from teatree.loops.dream.transcript_extract import high_signal_lines


def _memory_snippet(name: str, body: str) -> WeightedSnippet:
    return WeightedSnippet(path=Path(f"/memory/{name}"), kind="memory", weight=90, text=body)


def _transcript_snippet(name: str, body: str) -> WeightedSnippet:
    return WeightedSnippet(path=Path(f"/sessions/{name}"), kind="main", weight=100, text=body)


def _extract(*snippets: WeightedSnippet) -> ConsolidationExtract:
    return ConsolidationExtract(snippets=tuple(snippets))


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


UMBRELLA = "https://github.com/souliane/teatree/issues/2663"


def _fake_host(*, body: str = "## Open gaps\n") -> CodeHostBackend:
    host = MagicMock(spec=CodeHostBackend)
    host.search_open_issues.return_value = []
    host.get_issue.return_value = {"body": body}
    host.update_issue.return_value = {"number": 2663}
    return host


def _stateful_fake_host() -> CodeHostBackend:
    """A fake umbrella whose ``get_issue`` reflects the prior ``update_issue`` writes.

    ``_fake_host`` pins a fixed body, so a repeat pass re-adds the same checkbox and
    never reaches the already-promoted dedup path a multi-pass test is about.
    """
    host = MagicMock(spec=CodeHostBackend)
    state = {"body": "## Open gaps\n"}
    host.search_open_issues.return_value = []
    host.get_issue.side_effect = lambda _issue_url: {"body": state["body"]}

    def _update(*, body: str, **_rest: str) -> dict[str, int]:
        state["body"] = body
        return {"number": 2663}

    host.update_issue.side_effect = _update
    return host


def _recurrence(rule_identity: str, *, evidence: str = "") -> ComplianceFinding:
    return ComplianceFinding(
        rule_source=RuleSource.MEMORY,
        rule_identity=rule_identity,
        evidence=evidence or f"violated {rule_identity}",
        is_recurrence=True,
    )


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

    def test_single_incidental_shared_token_is_not_a_recurrence(self) -> None:
        # F6.5: one shared token is noise. Requiring >=2 distinctive shared tokens
        # stops a correction from being misattributed to an arbitrary memory that
        # merely happens to share a common word — the correction is still a compliance
        # failure, just a first-occurrence directive, not a memory recurrence.
        memory = (
            "name: feedback_provision_lease\nThe worktree provision lease claims a pid guard before owner liveness.\n"
        )
        violation = (
            '{"type": "user", "content": "stop touching the worktree without asking, you do not follow instructions!!"}'
        )
        extract = _extract(
            _memory_snippet("feedback_provision_lease.md", memory),
            _transcript_snippet("session-x.jsonl", violation),
        )
        findings = detect_compliance_failures(extract)
        assert findings  # still a compliance failure
        assert all(not f.is_recurrence for f in findings)
        assert all(f.rule_source is RuleSource.IN_SESSION for f in findings)

    def test_best_overlap_memory_wins_not_an_arbitrary_first_match(self) -> None:
        # F6.5: attribution is BEST-match, not first-token-wins. The correction shares
        # two distinctive tokens with feedback_beta and only one with feedback_alpha,
        # so the recurrence attributes to feedback_beta.
        mem_a = "name: feedback_alpha\nThe alphaword lesson about widgets.\n"
        mem_b = "name: feedback_beta\nThe alphaword and betaword handling.\n"
        violation = (
            '{"type": "user", "content": "stop ignoring the alphaword and betaword rule again, '
            'you do not follow instructions!!"}'
        )
        extract = _extract(
            _memory_snippet("feedback_alpha.md", mem_a),
            _memory_snippet("feedback_beta.md", mem_b),
            _transcript_snippet("session-y.jsonl", violation),
        )
        recurrences = [f for f in detect_compliance_failures(extract) if f.is_recurrence]
        assert len(recurrences) == 1
        assert recurrences[0].rule_identity == "feedback_beta"

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
    """A recurrence rides the umbrella + a scheduled gate/eval fix, never a memory."""

    def _recurrence(self, identity: str = "feedback_askuserquestion_overuse") -> ComplianceFinding:
        return ComplianceFinding(
            rule_source=RuleSource.MEMORY,
            rule_identity=identity,
            evidence="AskUserQuestion fired again despite the memory",
            is_recurrence=True,
        )

    def test_one_recurrence_upserts_a_checkbox_and_schedules_a_fix(self) -> None:
        from teatree.core.models.task import Task  # noqa: PLC0415 — deferred: ORM/app-registry, test-local import

        host = _fake_host()
        batch = PromotionBatch()
        outcomes = escalate_recurrences([self._recurrence()], batch=batch, umbrella_url=UMBRELLA)
        assert len(outcomes) == 1
        assert outcomes[0].filed is True
        # Nothing is written/scheduled until the pass mints its single batch ticket.
        host.create_issue.assert_not_called()
        host.update_issue.assert_not_called()

        batch_outcome = bp_module.promote_batch(host, umbrella_url=UMBRELLA, batch=batch)
        assert batch_outcome.scheduled is True
        host.create_issue.assert_not_called()
        host.update_issue.assert_called_once()
        _, kwargs = host.update_issue.call_args
        # The checkbox title prescribes a STRUCTURAL fix (a gate or an eval).
        title = kwargs["body"].lower()
        assert "gate" in title or "eval" in title
        assert Ticket.objects.filter(extra__dream_gap_batch__isnull=False).exists()
        assert Task.objects.filter(phase="coding").exists()

    def test_two_recurrences_of_the_same_rule_promote_one_gap(self) -> None:
        batch = PromotionBatch()
        outcomes = escalate_recurrences([self._recurrence(), self._recurrence()], batch=batch, umbrella_url=UMBRELLA)
        filed = [o for o in outcomes if o.filed]
        assert len(filed) == 1
        assert len(batch.pending) == 1

    def test_a_pre_existing_checkbox_with_no_backing_ticket_is_not_duplicated(self) -> None:
        # A bare checkbox line with no backing Ticket names nothing in flight, so the
        # gap is queued and its ticket minted fresh — but the umbrella write dedups by
        # marker, so no NEW line is appended (nothing to write — the box is already
        # there), while the fix still gets a real ticket.
        marker = "<!-- dream-gap compliance-recurrence-feedback_askuserquestion_overuse -->"
        existing = f"## Open gaps\n- [ ] Compliance recurrence ... {marker}\n"
        host = _fake_host(body=existing)
        batch = PromotionBatch()
        escalate_recurrences([self._recurrence()], batch=batch, umbrella_url=UMBRELLA)
        outcome = bp_module.promote_batch(host, umbrella_url=UMBRELLA, batch=batch)
        assert outcome.checkboxes_added == 0
        host.update_issue.assert_not_called()
        assert Ticket.objects.filter(extra__dream_gap_batch__isnull=False).exists()

    def test_non_recurrence_findings_are_never_escalated(self) -> None:
        first_occurrence = ComplianceFinding(
            rule_source=RuleSource.IN_SESSION,
            rule_identity="rename-public-api",
            evidence="renamed the API again",
            is_recurrence=False,
        )
        batch = PromotionBatch()
        outcomes = escalate_recurrences([first_occurrence], batch=batch, umbrella_url=UMBRELLA)
        assert outcomes == []
        assert batch.pending == []


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

    def test_a_failed_record_write_leaves_no_snapshot_claiming_violations_it_cannot_show(self) -> None:
        # The §4 gate reads the snapshot's counts and the recurrence detection reads the
        # rows, so a snapshot that outlived its detail rows is a permanently unrepairable
        # audit trail — nothing re-derives the findings of a pass that already happened.
        findings = [
            ComplianceFinding(
                rule_source=RuleSource.MEMORY,
                rule_identity="feedback_a",
                evidence="violated a",
                is_recurrence=True,
            )
        ]
        with (
            patch.object(InstructionComplianceRecord.objects, "bulk_create", side_effect=OperationalError("disk I/O")),
            pytest.raises(OperationalError),
        ):
            persist_compliance_pass(findings, instructions_observed=10)

        assert not InstructionComplianceSnapshot.objects.exists()

    def test_escalated_recurrence_record_carries_the_escalation_url(self) -> None:
        host = _fake_host()
        finding = ComplianceFinding(
            rule_source=RuleSource.MEMORY,
            rule_identity="feedback_a",
            evidence="violated a",
            is_recurrence=True,
        )
        snapshot = persist_compliance_pass([finding], instructions_observed=4)
        # Stamping fires as soon as the gap is QUEUED (rides the umbrella once the
        # pass's batch is minted) — not gated on promote_batch having run yet.
        run_compliance_escalation(
            snapshot=snapshot, findings=[finding], host=host, dry_run=False, batch=PromotionBatch()
        )
        row = InstructionComplianceRecord.objects.get(snapshot=snapshot, rule_identity="feedback_a")
        assert row.remediation == RemediationKind.ESCALATION
        # The escalation is now the standing umbrella (the recurrence rides it + a coding task).
        assert row.escalation_url == UMBRELLA

    def test_a_recurrence_stays_stamped_escalated_across_passes_once_promoted(self) -> None:
        # Idempotency is invisible to a single pass: pass 1 promotes the gap for real
        # (mints the batch ticket); pass 2 detects the SAME recurrence, finds it
        # already covered by that in-flight ticket, and must keep reading ESCALATION
        # without double-adding a checkbox or minting a second ticket (#4776).
        host = _stateful_fake_host()
        finding = _recurrence("feedback_a")

        batch1 = PromotionBatch()
        snapshot1 = persist_compliance_pass([finding], instructions_observed=4)
        run_compliance_escalation(snapshot=snapshot1, findings=[finding], host=host, dry_run=False, batch=batch1)
        bp_module.promote_batch(host, umbrella_url=UMBRELLA, batch=batch1)
        row1 = InstructionComplianceRecord.objects.get(snapshot=snapshot1, rule_identity="feedback_a")
        assert row1.remediation == RemediationKind.ESCALATION

        batch2 = PromotionBatch()
        snapshot2 = persist_compliance_pass([finding], instructions_observed=4)
        run_compliance_escalation(snapshot=snapshot2, findings=[finding], host=host, dry_run=False, batch=batch2)
        row2 = InstructionComplianceRecord.objects.get(snapshot=snapshot2, rule_identity="feedback_a")
        assert row2.remediation == RemediationKind.ESCALATION
        assert row2.escalation_url == UMBRELLA
        assert batch2.pending == []  # already covered — not re-queued
        assert host.update_issue.call_count == 1  # one checkbox write total, from pass 1

    def test_every_row_of_one_rule_is_stamped_not_just_the_first(self) -> None:
        # One row per FINDING is persisted, but escalation dedups to one outcome per
        # rule_identity — so a sibling row of the same rule would keep reading NONE for
        # a recurrence that rides the umbrella (#4176).
        host = _fake_host()
        findings = [_recurrence("feedback_a", evidence="violated 0"), _recurrence("feedback_a", evidence="violated 1")]
        snapshot = persist_compliance_pass(findings, instructions_observed=4)
        run_compliance_escalation(
            snapshot=snapshot, findings=findings, host=host, dry_run=False, batch=PromotionBatch()
        )
        rows = list(InstructionComplianceRecord.objects.filter(snapshot=snapshot, rule_identity="feedback_a"))
        assert len(rows) == 2
        assert {row.remediation for row in rows} == {RemediationKind.ESCALATION}
        assert {row.escalation_url for row in rows} == {UMBRELLA}

    def test_a_newly_withheld_title_does_not_unstamp_a_riding_recurrence(self) -> None:
        # The banned-terms ruleset is versioned: tonight's pass can withhold a title it
        # promoted last night, while that checkbox and its coding task stay live (#4176).
        host = _stateful_fake_host()
        finding = _recurrence("feedback_a")
        batch1 = PromotionBatch()
        first = persist_compliance_pass([finding], instructions_observed=4)
        run_compliance_escalation(snapshot=first, findings=[finding], host=host, dry_run=False, batch=batch1)
        bp_module.promote_batch(host, umbrella_url=UMBRELLA, batch=batch1)

        batch2 = PromotionBatch()
        second = persist_compliance_pass([finding], instructions_observed=4)
        with patch("teatree.loops.dream.umbrella_ledger.banned_terms_scanner.scan_text", return_value="customer-name"):
            run_compliance_escalation(snapshot=second, findings=[finding], host=host, dry_run=False, batch=batch2)
        row = InstructionComplianceRecord.objects.get(snapshot=second, rule_identity="feedback_a")
        assert row.remediation == RemediationKind.ESCALATION
        assert row.escalation_url == UMBRELLA
        assert host.update_issue.call_count == 1


class RunComplianceMeasurementTestCase(TestCase):
    """Measurement runs on EVERY pass (default ON): it persists a snapshot, never files."""

    def _violation_extract(self) -> ConsolidationExtract:
        return _extract(
            _memory_snippet("feedback_askuserquestion_overuse.md", _MEMORY_BODY),
            _transcript_snippet("session-a.jsonl", _VIOLATION_TURN),
        )

    def test_measurement_persists_a_snapshot_and_carries_findings(self) -> None:
        measurement = run_compliance_measurement(extract=self._violation_extract(), dry_run=False)
        assert measurement.snapshot is not None
        assert InstructionComplianceSnapshot.objects.count() == 1
        assert any(f.is_recurrence for f in measurement.findings)
        assert "compliance 1 violation(s)" in measurement.summary

    def test_dry_run_measurement_persists_no_rows(self) -> None:
        # RED before the split: the old run_compliance_phase persisted the snapshot
        # UNCONDITIONALLY (only escalation honoured dry_run), so a --dry-run preview
        # wrote real rows. Measurement must record nothing under dry_run.
        measurement = run_compliance_measurement(extract=self._violation_extract(), dry_run=True)
        assert measurement.snapshot is None
        assert InstructionComplianceSnapshot.objects.count() == 0
        assert InstructionComplianceRecord.objects.count() == 0
        # The findings are still surfaced so a downstream escalation could act on them.
        assert any(f.is_recurrence for f in measurement.findings)

    def test_zero_instructions_records_nothing_and_warns(self) -> None:
        with self.assertLogs("teatree.loops.dream.compliance", level="WARNING") as logs:
            measurement = run_compliance_measurement(extract=_extract(), dry_run=False)
        assert measurement.snapshot is None
        assert measurement.summary == ""
        assert InstructionComplianceSnapshot.objects.count() == 0
        assert any("0 instructions" in line for line in logs.output)


class RunComplianceEscalationTestCase(TestCase):
    """Escalation is the default-OFF, --full-gated half: it files recurrences, never a memory."""

    def _recurrence(self) -> ComplianceFinding:
        return ComplianceFinding(
            rule_source=RuleSource.MEMORY,
            rule_identity="feedback_askuserquestion_overuse",
            evidence="AskUserQuestion fired again despite the memory",
            is_recurrence=True,
        )

    def test_recurrence_is_queued_and_the_summary_reports_it(self) -> None:
        # #4776: nothing is written here — a recurrence is QUEUED into the pass's
        # batch; the single forge write happens once, later, in promote_batch.
        host = _fake_host()
        batch = PromotionBatch()
        summary = run_compliance_escalation(
            snapshot=None, findings=[self._recurrence()], host=host, dry_run=False, batch=batch
        )
        assert summary == "; escalated 1/1 compliance recurrence(s)"
        assert len(batch.pending) == 1
        host.update_issue.assert_not_called()

    def test_no_host_is_a_skip_warning_not_a_raise(self) -> None:
        summary = run_compliance_escalation(
            snapshot=None, findings=[self._recurrence()], host=None, dry_run=False, batch=PromotionBatch()
        )
        assert "no teatree code host resolved" in summary

    def test_dry_run_previews_the_count_but_queues_nothing(self) -> None:
        host = _fake_host()
        batch = PromotionBatch()
        summary = run_compliance_escalation(
            snapshot=None, findings=[self._recurrence()], host=host, dry_run=True, batch=batch
        )
        # The preview reports what a real run WOULD escalate (1/1), not a bare zero…
        assert summary == "; escalated 1/1 compliance recurrence(s)"
        # …while nothing is actually queued or written.
        assert batch.pending == []
        host.update_issue.assert_not_called()

    def test_no_recurrence_returns_empty_clause(self) -> None:
        first_occurrence = ComplianceFinding(
            rule_source=RuleSource.IN_SESSION,
            rule_identity="rename-public-api",
            evidence="renamed the API again",
            is_recurrence=False,
        )
        host = _fake_host()
        summary = run_compliance_escalation(
            snapshot=None, findings=[first_occurrence], host=host, dry_run=False, batch=PromotionBatch()
        )
        assert summary == ""
        host.update_issue.assert_not_called()


class BuildComplianceSnapshotTestCase(TestCase):
    """The detect→snapshot helper counts observed instructions across the corpus."""

    def test_observed_count_excludes_non_rule_memories(self) -> None:
        # The denominator is the instruction surface actually exercised. Counting a
        # per-ticket state log as an instruction inflated it toward a flattering rate.
        log = (
            "---\nname: ticket-9001-notes\nmetadata:\n  type: project\n---\n"
            "The sweep is still waiting on the fold approval.\n"
        )
        rule_only = build_compliance_snapshot(_extract(_memory_snippet("feedback_x.md", _MEMORY_BODY)))
        with_log = build_compliance_snapshot(
            _extract(
                _memory_snippet("feedback_x.md", _MEMORY_BODY),
                _memory_snippet("ticket-9001-notes.md", log),
            )
        )
        assert with_log.instructions_observed == rule_only.instructions_observed

    def test_a_dispatch_brief_never_mints_a_recurrence(self) -> None:
        # The exact shape that minted this ticket's own gap: a headless brief quoting
        # rule prose, scored against a memory that states no rule.
        log = (
            "---\nname: ticket-9001-fold-into-9002-pending-approval\nmetadata:\n  type: project\n---\n"
            "The sweep proposes folding ticket 9001 into 9002; approval is pending. "
            "Do not re-propose it. Re-check the worktree and the branch review state.\n"
        )
        brief = (
            '{"role": "user"} Work on ticket 9001. Issue: <issue-url> Current phase: coding '
            "Reason: the fold approval is pending; do not re-propose it, and never "
            "force-push the worktree branch under review.\n"
        )
        result = build_compliance_snapshot(
            _extract(
                _memory_snippet("ticket-9001-fold-into-9002-pending-approval.md", log),
                _transcript_snippet("session-a.jsonl", brief),
            )
        )
        assert not [f for f in result.findings if f.is_recurrence]

    def test_a_tool_result_never_mints_a_recurrence(self) -> None:
        # Relayed shell output shares the memory's distinctive tokens and carries every
        # correction cue, so only the role tag separates it from a typed complaint.
        raw = json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "tool_use_id": "toolu_1",
                            "type": "tool_result",
                            "content": "AskUserQuestion: routine obstacles must not fire the gate — do not retry",
                        }
                    ],
                },
            }
        )
        result = build_compliance_snapshot(
            _extract(
                _memory_snippet("feedback_askuserquestion_overuse.md", _MEMORY_BODY),
                _transcript_snippet("session-a.jsonl", high_signal_lines(raw)),
            )
        )
        assert result.findings == ()

    def test_a_harness_hook_feedback_turn_never_mints_a_recurrence(self) -> None:
        raw = json.dumps(
            {
                "type": "user",
                "isMeta": True,
                "message": {
                    "role": "user",
                    "content": "Stop hook feedback: AskUserQuestion for routine obstacles is not allowed — do not stop",
                },
            }
        )
        result = build_compliance_snapshot(
            _extract(
                _memory_snippet("feedback_askuserquestion_overuse.md", _MEMORY_BODY),
                _transcript_snippet("session-a.jsonl", high_signal_lines(raw)),
            )
        )
        assert result.findings == ()

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
