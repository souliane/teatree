"""Walk captured sessions and write the conversation-audit ledger (#1861).

This is the orchestration layer over the #2192 data layer and the existing
session readers. For each captured session it composes — never reimplements —
the production readers:

*   :func:`teatree.eval.transcript_conformance.replay` over the wider
    :data:`~teatree.eval.transcript_conformance.AUDIT_REGISTRY` (the ship-blocking
    GREEN subset plus the deferred AMBER-tier policy invariants) yields the
    behavioural :class:`InvariantOutcome` list;
*   :func:`teatree.eval.gate_failures.extract_gate_failures` /
    :func:`~teatree.eval.gate_failures.classify_gate_failure` yield the
    preventable gate-failure slugs;
*   when a session matches a ground-truth :class:`CorpusLabel`,
    :func:`teatree.eval.corpus_grade.grade` (with the anti-circular
    :func:`~teatree.eval.corpus_grade.assert_independent_oracle` guard) grades it
    against the external label, and the categorical
    ``(outcome_axis, expected_outcome, predicted_outcome)`` triple comes from the
    label + the grade.

An un-labelled session still gets invariant + gate-failure analysis on a synthetic
``conformance`` axis (``clean`` / ``one_shot`` / ``sustained``) and is *nominated
for labelling* when it hit a preventable gate failure or graded AMBER — exactly
the sessions a human labeller should turn into new ground truth.

The LLM judge is INJECTED as a :data:`~teatree.eval.report.JudgeGrader`; the
audit never constructs :class:`~teatree.eval.judge.ClaudeJudge` itself, so a test
passes a fake and the network is never touched.

Each audited session produces ONE *unsaved* :class:`SessionAuditRecord`; the
caller (:func:`run_conversation_audit` / :func:`audit_corpus`) decides when to
persist the batch via :func:`teatree.eval.audit_persistence.persist_audit`.

PRIVACY by construction: a record carries ONLY ids, indexes, slugs, and
categorical labels — the invariant offending *index* (never the offending
command), the gate-identity *slug* (never the blockingError message), and the
categorical outcome. No tool input, prompt, or hook payload is ever copied onto
the ledger.
"""

import dataclasses
from collections.abc import Sequence
from enum import StrEnum
from itertools import starmap
from pathlib import Path

from teatree.core.models import EvalVerdict, InvariantOutcome, SessionAuditRecord
from teatree.eval.audit_persistence import persist_audit
from teatree.eval.corpus_grade import assert_independent_oracle, grade
from teatree.eval.corpus_loader import CORPUS_DIR, discover_corpus
from teatree.eval.corpus_models import CorpusLabel
from teatree.eval.gate_failures import GateVerdict, classify_gate_failure, extract_gate_failures
from teatree.eval.report import JudgeGrader, ScenarioResult
from teatree.eval.session_transcript import SessionEvent, parse_session_jsonl
from teatree.eval.transcript_conformance import AUDIT_REGISTRY, Invariant, InvariantResult, replay

_CONFORMANCE_AXIS = "conformance"
_CLEAN = "clean"
#: Predicted-outcome sentinel for a judge-oracle entry graded with no judge: a
#: value that is never an expected outcome, so it never lands on the
#: confusion-matrix diagonal as a (vacuous) correct prediction.
_UNJUDGED = "unjudged"


class BehaviorPattern(StrEnum):
    """How many distinct problems a session exhibited.

    The signal is a COUNT of distinct offending signals (failing invariants +
    preventable gate failures), not a same-problem recurrence: ``CLEAN`` — zero;
    ``ONE_SHOT`` — exactly one distinct violation or preventable gate failure;
    ``SUSTAINED`` — more than one distinct violation or preventable gate failure
    (two unrelated single slips already count as SUSTAINED), a broader-trouble
    signal the audit weights more heavily than an isolated lapse.
    """

    CLEAN = "clean"
    ONE_SHOT = "one_shot"
    SUSTAINED = "sustained"


@dataclasses.dataclass(frozen=True)
class AuditInput:
    """One captured session to audit.

    ``label`` is the matched ground-truth :class:`CorpusLabel` when the session is
    in the corpus, else ``None`` (an un-labelled session still gets conformance +
    gate-failure analysis and may be nominated).
    """

    session_id: str
    events: list[SessionEvent]
    label: CorpusLabel | None = None


@dataclasses.dataclass(frozen=True)
class _Analysis:
    """The session-wide signal computed once, shared by both record builders."""

    invariant_outcomes: list[InvariantOutcome]
    gate_slugs: list[str]
    pattern: "BehaviorPattern"


def audit_session(
    audit_input: AuditInput,
    *,
    judge: JudgeGrader | None = None,
    invariants: tuple[Invariant, ...] = AUDIT_REGISTRY,
) -> SessionAuditRecord:
    """Audit one captured session into a single UNSAVED :class:`SessionAuditRecord`.

    Runs the conformance invariants and the gate-failure extractor over every
    session; additionally grades a corpus-matched session against its external
    label (refusing a circular matcher oracle first). The categorical triple and
    verdict come from the label + grade for a matched session, or from the
    conformance pattern for an un-labelled one.
    """
    invariant_outcomes = _invariant_outcomes(audit_input.events, invariants)
    gate_slugs = _preventable_gate_slugs(audit_input.events, session_id=audit_input.session_id)
    violations = sum(1 for o in invariant_outcomes if not o["ok"])
    analysis = _Analysis(invariant_outcomes, gate_slugs, _behavior_pattern(violations + len(gate_slugs)))
    label = audit_input.label
    if label is not None:
        return _graded_record(audit_input, label, analysis, judge=judge)
    return _unlabelled_record(audit_input, analysis)


def classify_behavior_pattern(record: SessionAuditRecord) -> BehaviorPattern:
    """Classify a record's invariant + gate-failure signal as clean / one-shot / sustained.

    Pure over the persisted signal (``invariant_results`` + ``gate_failure_slugs``)
    so it reads identically off an in-memory unsaved record and a row re-read from
    the ledger.
    """
    violations = sum(1 for o in record.invariant_results if not o.get("ok", True))
    return _behavior_pattern(violations + len(record.gate_failure_slugs))


def run_conversation_audit(
    inputs: Sequence[AuditInput],
    *,
    judge: JudgeGrader | None = None,
    persist: bool = True,
    git_sha: str | None = None,
) -> list[SessionAuditRecord]:
    """Audit a batch of captured sessions; persist the records in one transaction.

    Each session yields one unsaved record; when ``persist`` the batch is written
    via :func:`persist_audit` (one atomic txn, ``git_sha`` stamped). With
    ``persist=False`` the unsaved records are returned for inspection.
    """
    records = [audit_session(audit_input, judge=judge) for audit_input in inputs]
    return persist_audit(records, git_sha=git_sha) if persist else records


def audit_corpus(
    *,
    directory: Path | None = None,
    judge: JudgeGrader | None = None,
    persist: bool = True,
    git_sha: str | None = None,
) -> list[SessionAuditRecord]:
    """Audit every shipped corpus capture against its ground-truth label.

    Discovers the corpus from disk, pairs each label with its sibling
    ``<entry_id>.session.jsonl`` capture, and audits the matched session — the
    corpus-matched path of :func:`run_conversation_audit`.
    """
    root = CORPUS_DIR if directory is None else directory
    inputs = [
        AuditInput(
            session_id=label.source_session_id or label.entry_id,
            events=parse_session_jsonl((root / f"{label.entry_id}.session.jsonl").read_text(encoding="utf-8")),
            label=label,
        )
        for label in discover_corpus(directory)
    ]
    return run_conversation_audit(inputs, judge=judge, persist=persist, git_sha=git_sha)


def _graded_record(
    audit_input: AuditInput,
    label: CorpusLabel,
    analysis: _Analysis,
    *,
    judge: JudgeGrader | None,
) -> SessionAuditRecord:
    assert_independent_oracle(label)
    if _needs_judge_but_absent(label, judge):
        return _unjudged_record(audit_input, label, analysis)
    result = grade(label, audit_input.events, judge=judge)
    verdict = _verdict(result)
    nominated = verdict is EvalVerdict.FAIL or _has_conformance_signal(analysis)
    return SessionAuditRecord(
        session_id=audit_input.session_id,
        corpus_entry_id=label.entry_id,
        outcome_axis=label.outcome_axis,
        expected_outcome=label.expected_outcome,
        predicted_outcome=_predicted_outcome(label, verdict),
        verdict=verdict,
        oracle=label.oracle,
        judge_rationale=_judge_rationale(result),
        invariant_results=analysis.invariant_outcomes,
        gate_failure_slugs=analysis.gate_slugs,
        nominated_for_label=nominated,
    )


def _needs_judge_but_absent(label: CorpusLabel, judge: JudgeGrader | None) -> bool:
    """True when grading *label* requires a judge that was not injected.

    A ``judge`` oracle (and a ``both`` oracle with no matchers to fall back on)
    can only be decided by the LLM judge. With ``judge=None`` — the audit CLI's
    free-and-deterministic default — grading it would be vacuous (no matcher,
    no judge → ``report.evaluate`` returns a forced PASS), so the entry must
    SKIP rather than land a fake correct prediction on the diagonal. Mirrors the
    ``cli/eval/corpus.py`` guard.
    """
    if judge is not None:
        return False
    return label.oracle == "judge" or (label.oracle == "both" and not label.matchers)


def _unjudged_record(audit_input: AuditInput, label: CorpusLabel, analysis: _Analysis) -> SessionAuditRecord:
    """A SKIP record for a judge-required entry graded with no judge.

    ``predicted_outcome`` is the :data:`_UNJUDGED` sentinel — never the expected
    value — so the row stays off the confusion-matrix diagonal and never inflates
    accuracy. A judge-required entry is always nominated: it carries unresolved
    ground truth a human (with a judge) should still grade.
    """
    return SessionAuditRecord(
        session_id=audit_input.session_id,
        corpus_entry_id=label.entry_id,
        outcome_axis=label.outcome_axis,
        expected_outcome=label.expected_outcome,
        predicted_outcome=_UNJUDGED,
        verdict=EvalVerdict.SKIP,
        oracle=label.oracle,
        invariant_results=analysis.invariant_outcomes,
        gate_failure_slugs=analysis.gate_slugs,
        nominated_for_label=True,
    )


def _unlabelled_record(audit_input: AuditInput, analysis: _Analysis) -> SessionAuditRecord:
    return SessionAuditRecord(
        session_id=audit_input.session_id,
        corpus_entry_id="",
        outcome_axis=_CONFORMANCE_AXIS,
        expected_outcome=_CLEAN,
        predicted_outcome=str(analysis.pattern),
        verdict=EvalVerdict.SKIP,
        oracle="invariant",
        invariant_results=analysis.invariant_outcomes,
        gate_failure_slugs=analysis.gate_slugs,
        nominated_for_label=_has_conformance_signal(analysis),
    )


def _has_conformance_signal(analysis: _Analysis) -> bool:
    """A session worth nominating: any invariant violation or any preventable gate failure."""
    return analysis.pattern is not BehaviorPattern.CLEAN or bool(analysis.gate_slugs)


def _invariant_outcomes(events: list[SessionEvent], invariants: tuple[Invariant, ...]) -> list[InvariantOutcome]:
    results = replay(events, invariants)
    return list(starmap(_outcome, zip(invariants, results, strict=True)))


def _outcome(invariant: Invariant, result: InvariantResult) -> InvariantOutcome:
    return {"invariant_id": invariant.id, "ok": result.ok, "offending_index": result.offending_index}


def _preventable_gate_slugs(events: list[SessionEvent], *, session_id: str) -> list[str]:
    failures = extract_gate_failures(events, session_id=session_id)
    return [f.gate for f in failures if classify_gate_failure(f) is GateVerdict.PREVENTABLE]


def _behavior_pattern(signals: int) -> BehaviorPattern:
    if signals == 0:
        return BehaviorPattern.CLEAN
    return BehaviorPattern.ONE_SHOT if signals == 1 else BehaviorPattern.SUSTAINED


def _verdict(result: ScenarioResult) -> EvalVerdict:
    return EvalVerdict(result.verdict)


def _predicted_outcome(label: CorpusLabel, verdict: EvalVerdict) -> str:
    """The categorical prediction for a graded session: the expected value on a pass, its negation on a fail.

    Only reached from :func:`_graded_record`, where a captured run grades PASS or
    FAIL — a corpus capture's terminal reason is never ``skipped:``, so SKIP is not
    a case here.
    """
    return label.expected_outcome if verdict is EvalVerdict.PASS else f"not_{label.expected_outcome}"


def _judge_rationale(result: ScenarioResult) -> str:
    return result.judge.rationale if result.judge is not None and not result.judge.skipped else ""
