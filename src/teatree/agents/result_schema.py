"""Structured output schema for agent task results.

Agents return JSON matching this schema. Any agent that can produce JSON works —
Claude structured output just makes schema compliance guaranteed.

Phase-specific evidence requirements (#1284 / codex #1282-6): a successful
phase task must carry concrete evidence the work actually happened — not just
a summary string. ``PHASE_REQUIRED_EVIDENCE`` names the per-phase fields the
agent must supply (in addition to ``summary``); ``_record_success`` consults
this map and refuses to complete the task when the claim has no evidence. The
"DM sent successfully but didn't deliver" false-positive class is exactly the
shape this prevents: a one-line summary advancing the FSM with no underlying
proof.
"""

from collections.abc import Callable
from typing import TypedDict, cast

from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models.mechanism_sketch import MechanismSketchDict


class FileChange(TypedDict, total=False):
    path: str
    action: str  # "created", "modified", "deleted"
    lines_added: int
    lines_removed: int


class TestResult(TypedDict, total=False):
    name: str
    passed: bool
    duration_seconds: float
    error: str


class ReviewFinding(TypedDict, total=False):
    severity: str
    summary: str
    file: str
    line: int


class ArticleSuggestion(TypedDict, total=False):
    """One news-scan candidate a shell-denied scanning_news agent hands back (#9).

    The headless scanning_news phase cannot run the ``t3`` CLI to enqueue
    candidates, so it RETURNS these instead: the recorder creates one
    :class:`~teatree.core.models.pending_article_suggestion.PendingArticleSuggestion`
    per candidate behind the ask-gate (idempotent by ``url``). ``rationale`` is
    the one-line why-this-matters that becomes the row's summary.
    """

    title: str
    url: str
    rationale: str


class AnswerEnvelope(TypedDict, total=False):
    """A shell-denied answering agent's drafted reply, handed back for approval (#9).

    The headless answering phase cannot post on the user's behalf, so it
    RETURNS the draft: the recorder routes ``text`` through the
    :class:`~teatree.core.models.deferred_question.DeferredQuestion` approval
    path (correlated to the task), and the orchestrator posts on confirmation.
    ``thread_ref`` is the inbound thread the reply targets.
    """

    text: str
    thread_ref: str


class ReviewVerdictEnvelope(TypedDict, total=False):
    """A reviewing-phase agent's typed verdict, recorded server-side (corr-11).

    A headless reviewing phase is denied the shell (PR-11), so it cannot run
    ``t3 <overlay> review record``. It RETURNS this instead: the orchestrator
    (a different actor) records the ``ReviewVerdict`` from it, so maker≠checker
    holds by construction. ``reviewed_sha`` is the full 40-char SHA the review
    bound to; ``verdict`` is ``merge_safe`` / ``hold``.
    """

    verdict: str
    reviewed_sha: str
    reviewer_identity: str
    gh_verify_result: str
    blast_class: str
    findings: list[ReviewFinding]


class CriticItemVerdictDict(TypedDict, total=False):
    slug: str
    status: str  # "pass" | "fail" | "instrumentation_gap"
    citation: str


class CriticVerdictEnvelope(TypedDict, total=False):
    """The autonomous user-proxy critic's typed verdict, recorded server-side (SELFCATCH-5).

    A headless critic phase is denied the shell, so it RETURNS this instead of
    recording a ``CriticVerdict`` itself: the orchestrator (``attempt_recorder`` →
    ``critic_gate.record_returned_critic_verdict``) records it, so maker≠checker
    holds by construction. ``items`` carries one per-rubric-item PASS/FAIL with a
    citation (an uncited pass is stored as ``instrumentation_gap``).
    """

    grader_identity: str
    items: list[CriticItemVerdictDict]


class DirectiveInterpretationEnvelope(TypedDict, total=False):
    """A directive interpreter's typed return, recorded server-side (north-star PR-6).

    A headless ``directive_interpreting`` phase is denied the shell, so it RETURNS
    this instead of writing the sketch itself: ``attempt_recorder`` →
    ``directive_interpret_gate.record_returned_directive_interpretation`` records the
    :class:`~teatree.core.models.mechanism_sketch.MechanismSketch` onto the
    ``Directive`` (maker≠checker — a different actor than the one that captured the
    text). The interpreter returns EITHER a ``sketch`` (→ ``INTERPRETED``) OR
    ``clarifying_questions`` when the directive is ambiguous (→ ``CLARIFYING``).
    """

    interpreter_identity: str
    constraint_statement: str
    sketch: MechanismSketchDict
    clarifying_questions: list[str]


class DirectiveCandidateEnvelope(TypedDict, total=False):
    """A quarantined reader's typed verdict, recorded server-side (#116 context firewall).

    The no-tools/no-creds ``directive_reading`` reader (:mod:`teatree.agents.reader_profile`)
    is denied every tool, so it RETURNS this instead of acting: the orchestrator
    (``directive_candidate_gate.record_returned_directive_candidate``) validates it —
    provenance cross-check + the Layer-2 schema — and mints the ``Directive`` from the
    SANITIZED ``normalized_constraint``, so no downstream tooled stage ever touches raw
    attacker text (maker≠checker). ``provenance`` is the reader's ECHOED trust tag; the
    recorder cross-checks it against the true source event and never trusts it as the
    taint source.
    """

    reader_identity: str
    is_directive: bool
    normalized_constraint: str
    scope_overlay: str
    cited_signal: str
    provenance: str


class AgentResult(TypedDict, total=False):
    """Structured result from an agent task execution.

    All fields are optional — agents report what they can. Phase-specific
    evidence requirements are enforced by ``_record_success`` against
    ``PHASE_REQUIRED_EVIDENCE``, not by the JSON schema itself, because the
    required field depends on the running phase.
    """

    summary: str
    plan_text: str
    files_modified: list[FileChange]
    tests_run: list[TestResult]
    tests_passed: int
    tests_failed: int
    decisions: list[str]
    review_verdict: ReviewVerdictEnvelope
    critic_verdict: "CriticVerdictEnvelope"
    directive_interpretation: "DirectiveInterpretationEnvelope"
    directive_candidate: "DirectiveCandidateEnvelope"
    article_suggestions: list[ArticleSuggestion]
    answer: AnswerEnvelope
    needs_user_input: bool
    user_input_reason: str
    next_steps: list[str]
    commands_executed: list[str]


RESULT_JSON_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "One-line summary of what the agent did."},
        "plan_text": {"type": "string", "description": "Full plan text produced by the planner agent."},
        "files_modified": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "action": {"type": "string", "enum": ["created", "modified", "deleted"]},
                    "lines_added": {"type": "integer"},
                    "lines_removed": {"type": "integer"},
                },
                "required": ["path", "action"],
            },
        },
        "tests_run": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "passed": {"type": "boolean"},
                    "duration_seconds": {"type": "number"},
                    "error": {"type": "string"},
                },
                "required": ["name", "passed"],
            },
        },
        "tests_passed": {"type": "integer"},
        "tests_failed": {"type": "integer"},
        "decisions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Design decisions the agent made during execution.",
        },
        "review_verdict": {
            "type": "object",
            "description": "A reviewing-phase agent's typed verdict, recorded server-side (corr-11).",
            "properties": {
                "verdict": {"type": "string", "enum": ["merge_safe", "hold"]},
                "reviewed_sha": {"type": "string", "description": "Full 40-char hex SHA the review bound to."},
                "reviewer_identity": {"type": "string"},
                "gh_verify_result": {"type": "string", "enum": ["green", "pending", "failed"]},
                "blast_class": {"type": "string", "enum": ["substrate", "logic", "docs"]},
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "severity": {"type": "string"},
                            "summary": {"type": "string"},
                            "file": {"type": "string"},
                            "line": {"type": "integer"},
                        },
                    },
                },
            },
            "required": ["verdict"],
        },
        "critic_verdict": {
            "type": "object",
            "description": "The autonomous user-proxy critic's typed verdict, recorded server-side (SELFCATCH-5).",
            "properties": {
                "grader_identity": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "slug": {"type": "string"},
                            "status": {"type": "string", "enum": ["pass", "fail", "instrumentation_gap"]},
                            "citation": {"type": "string"},
                        },
                    },
                },
            },
        },
        "directive_interpretation": {
            "type": "object",
            "description": "A directive interpreter's typed return, recorded server-side (north-star PR-6).",
            "properties": {
                "interpreter_identity": {"type": "string"},
                "constraint_statement": {"type": "string"},
                "sketch": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string"},
                        "setting_key": {"type": "string"},
                        "setting_type": {"type": "string"},
                        "neutral_default": {},
                        "policy_chokepoint": {"type": "string"},
                        "activation_scope": {"type": "string"},
                        "activation_value": {},
                        "rejected_alternatives": {"type": "array", "items": {"type": "string"}},
                        "acceptance_tests": {"type": "array", "items": {"type": "string"}},
                        "refactors": {"type": "array", "items": {"type": "string"}},
                        "behavior_probe": {"type": "string"},
                        "probe_none_reason": {"type": "string"},
                    },
                },
                "clarifying_questions": {"type": "array", "items": {"type": "string"}},
            },
        },
        "directive_candidate": {
            "type": "object",
            "description": "A quarantined reader's typed verdict, recorded server-side (#116 context firewall).",
            "properties": {
                "reader_identity": {"type": "string"},
                "is_directive": {"type": "boolean"},
                "normalized_constraint": {"type": "string"},
                "scope_overlay": {"type": "string"},
                "cited_signal": {"type": "string"},
                "provenance": {"type": "string"},
            },
        },
        "article_suggestions": {
            "type": "array",
            "description": "Candidate news articles a shell-denied scanning_news agent hands back for queuing.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["url"],
            },
        },
        "answer": {
            "type": "object",
            "description": "A shell-denied answering agent's drafted reply, handed back for approval-gated posting.",
            "properties": {
                "text": {"type": "string"},
                "thread_ref": {"type": "string"},
            },
            "required": ["text"],
        },
        "needs_user_input": {"type": "boolean"},
        "user_input_reason": {"type": "string"},
        "next_steps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Suggested follow-up actions.",
        },
        "commands_executed": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Shell commands the agent ran.",
        },
    },
    "additionalProperties": False,
}


#: Per-phase required evidence fields (#1284 / codex #1282-6). At least one
#: of these fields must be present AND non-empty in the agent's success
#: result, otherwise the phase recording is refused. Keys are canonical
#: phase tokens (``coding``/``testing``/``reviewing``/``shipping``/...).
#: Each value is the list of acceptable evidence fields — supplying ANY of
#: them satisfies the requirement (an "evidence is one of" check, not "all
#: of"). The minimum non-trivial assertion per phase: did the agent produce
#: something a human could verify after the fact?
#:
#: - ``coding``: at least one file change recorded.
#: - ``testing``: at least one test result OR a positive ``tests_passed``.
#: - ``reviewing``: at least one design decision recorded, OR a typed
#:   ``review_verdict`` returned for server-side recording (corr-11) — a
#:   headless reviewer denied the shell proves the review happened by the
#:   verdict it hands back, not only by a decision list.
#: - ``shipping``: at least one command executed (``git push``, ``gh pr``...).
#: - ``scanning_news``: at least one ``article_suggestion`` returned — the
#:   shell-denied scanner hands its candidates back through the envelope, so a
#:   summary-only run is a silently-dropped scan (#9), refused here.
#: - ``answering``: an ``answer`` draft returned — same shell-denied hand-back;
#:   a summary-only run dropped the drafted reply.
#:
#: Phases not in this map (``scoping``, ``retro``) carry no evidence
#: requirement — they are intentionally lightweight.
PHASE_REQUIRED_EVIDENCE: dict[str, tuple[str, ...]] = {
    "planning": ("plan_text",),
    "coding": ("files_modified",),
    "testing": ("tests_run", "tests_passed"),
    "reviewing": ("decisions", "review_verdict"),
    "critic_reviewing": ("critic_verdict",),
    "directive_interpreting": ("directive_interpretation",),
    "directive_reading": ("directive_candidate",),
    "shipping": ("commands_executed",),
    "scanning_news": ("article_suggestions",),
    "answering": ("answer",),
}


def required_evidence_for_phase(phase: str) -> tuple[str, ...]:
    """Return the accepted evidence fields for ``phase`` (empty if none required)."""
    return PHASE_REQUIRED_EVIDENCE.get(normalize_phase(phase), ())


type AgentResultBlob = dict[str, object]


def suggestion_url(item: object) -> str:
    """The persistable source URL of one article suggestion, or ``""`` if absent.

    The single URL extractor BOTH the evidence gate and ``record_result_envelope``
    call, so "the gate passed" and "the recorder wrote a row" cannot disagree on
    what counts as a real candidate — the #9 gate/recorder-drift hardening.
    """
    if not isinstance(item, dict):
        return ""
    return str(cast("ArticleSuggestion", item).get("url") or "").strip()


def answer_text(answer: object) -> str:
    """The persistable reply text of an answer envelope, or ``""`` if absent."""
    if not isinstance(answer, dict):
        return ""
    return str(cast("AnswerEnvelope", answer).get("text") or "").strip()


def candidate_carries_payload(envelope: object) -> bool:
    """Whether a directive-candidate envelope carries something the recorder persists (#116).

    The recorder mints a ``Directive`` ONLY for a directive verdict with a non-empty
    normalized constraint (an ``is_directive: False`` verdict, or a directive with no
    constraint, persists nothing). This predicate matches that exactly, so "the gate
    passed" and "the recorder wrote a row" cannot disagree — the #9 gate/recorder-drift
    class, applied to the reader channel.
    """
    if not isinstance(envelope, dict):
        return False
    typed = cast("DirectiveCandidateEnvelope", envelope)
    return typed.get("is_directive") is True and bool(str(typed.get("normalized_constraint") or "").strip())


def interpretation_carries_payload(envelope: object) -> bool:
    """Whether a directive-interpretation envelope carries something the recorder persists.

    A real interpret result is EITHER a non-empty ``sketch`` dict OR a non-empty
    ``clarifying_questions`` list. An envelope with only an ``interpreter_identity``
    would pass a coarse truthiness check yet be dropped by the recorder — the exact
    gate/recorder-drift class (#9), refused here.
    """
    if not isinstance(envelope, dict):
        return False
    typed = cast("DirectiveInterpretationEnvelope", envelope)
    sketch = typed.get("sketch")
    if isinstance(sketch, dict) and sketch:
        return True
    questions = typed.get("clarifying_questions")
    return isinstance(questions, list) and any(str(q).strip() for q in questions)


#: Channels whose "evidence present" test is stricter than coarse truthiness:
#: the field must carry what the recorder actually PERSISTS (a url-bearing
#: suggestion, a text-bearing answer). Without this a schema-violating-but-
#: nonempty hand-back (``[{"title": "x"}]`` / ``{"thread_ref": "x"}``) the
#: recorder drops entirely would pass the gate and COMPLETE the task over zero
#: persisted work — the exact silent-drop class #9 closes.
_FIELD_PERSISTS: dict[str, Callable[[object], bool]] = {
    "article_suggestions": lambda v: isinstance(v, list) and any(suggestion_url(item) for item in v),
    "answer": lambda v: bool(answer_text(v)),
    "directive_interpretation": interpretation_carries_payload,
    "directive_candidate": candidate_carries_payload,
}


def _field_carries_evidence(result: AgentResultBlob, field: str) -> bool:
    predicate = _FIELD_PERSISTS.get(field)
    if predicate is not None:
        return predicate(result.get(field))
    return bool(result.get(field))


def check_evidence(result: AgentResultBlob, phase: str) -> str:
    """Return an error message if *result* lacks required evidence, else ``""``.

    A field is "present" iff the result has the key AND its value is
    truthy (non-zero int, non-empty list/dict/string) — except the envelope
    channels in ``_FIELD_PERSISTS``, which require the value to carry what the
    recorder actually PERSISTS (a url-bearing suggestion / a text-bearing
    answer), so the gate can never pass an envelope the recorder would drop.
    Supplying ANY of the acceptable fields for ``phase`` satisfies the check —
    the requirement is "one of these, with real content", not "all of these".

    Sub-agent contracts that opt out of normal completion (``needs_user_input``
    handoffs) bypass the check: the agent is *not* claiming the phase is
    done, so demanding phase evidence would be incoherent.
    """
    if result.get("needs_user_input"):
        return ""
    accepted = required_evidence_for_phase(phase)
    if not accepted:
        return ""
    if any(_field_carries_evidence(result, field) for field in accepted):
        return ""
    joined = " | ".join(accepted)
    return (
        f"missing required evidence for phase '{phase}': result must include one of [{joined}] "
        f"with a non-empty value (codex #1282-6)"
    )
