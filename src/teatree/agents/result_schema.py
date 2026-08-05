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
from teatree.core.modelkit.review_contract import ENVELOPE_FINDINGS_RULE
from teatree.core.models.mechanism_sketch import MechanismSketchDict
from teatree.core.models.types import PlanAdequacy


class FileChange(TypedDict, total=False):
    path: str
    action: str  # "created", "modified", "deleted"
    lines_added: int
    lines_removed: int


class SingleTestResult(TypedDict, total=False):
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
    """One news-scan candidate a shell-denied scanning_news agent hands back (#9)."""

    title: str
    url: str
    rationale: str


class TriageRecommendation(TypedDict, total=False):
    """One assessed ``needs-triage`` issue a shell-denied triage_assessing agent hands back."""

    issue_url: str
    verdict: str
    suggested_labels: list[str]
    priority: str
    duplicate_of: str
    rationale: str


class AnswerEnvelope(TypedDict, total=False):
    """A shell-denied answering agent's drafted reply, handed back for approval (#9)."""

    text: str
    thread_ref: str


class ReviewVerdictEnvelope(TypedDict, total=False):
    """A reviewing-phase agent's typed verdict, recorded server-side (corr-11)."""

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
    """The autonomous user-proxy critic's typed verdict, recorded server-side (SELFCATCH-5)."""

    grader_identity: str
    items: list[CriticItemVerdictDict]


class DirectiveInterpretationEnvelope(TypedDict, total=False):
    """A directive interpreter's typed return, recorded server-side (north-star PR-6)."""

    interpreter_identity: str
    constraint_statement: str
    sketch: MechanismSketchDict
    clarifying_questions: list[str]


class DirectiveCandidateEnvelope(TypedDict, total=False):
    """A quarantined reader's typed verdict, recorded server-side (#116 context firewall)."""

    reader_identity: str
    is_directive: bool
    normalized_constraint: str
    scope_overlay: str
    cited_signal: str
    provenance: str


class AgentResult(TypedDict, total=False):
    """Structured result from an agent task execution."""

    summary: str
    plan_text: str
    base_sha: str
    adequacy: PlanAdequacy
    files_modified: list[FileChange]
    tests_run: list[SingleTestResult]
    tests_passed: int
    tests_failed: int
    decisions: list[str]
    review_verdict: ReviewVerdictEnvelope
    critic_verdict: "CriticVerdictEnvelope"
    directive_interpretation: "DirectiveInterpretationEnvelope"
    directive_candidate: "DirectiveCandidateEnvelope"
    article_suggestions: list[ArticleSuggestion]
    triage_recommendations: list[TriageRecommendation]
    answer: AnswerEnvelope
    needs_user_input: bool
    user_input_reason: str
    next_steps: list[str]
    commands_executed: list[str]


type JSONSchema = dict[str, object]

#: One :class:`~teatree.core.models.types.AdequacySection` — substantive ``content``
#: (free text, or a list of items for ``integration_seams``/``edge_cases``) OR an
#: explicit reasoned ``none_reason`` negative. Shared by all four required sections
#: of the ``adequacy`` manifest so the schema cannot drift section-to-section.
_ADEQUACY_SECTION_SCHEMA: JSONSchema = {
    "type": "object",
    "properties": {
        "content": {"type": ["string", "array"], "items": {"type": "string"}},
        "none_reason": {"type": "string"},
    },
}

RESULT_JSON_SCHEMA: JSONSchema = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "One-line summary of what the agent did."},
        "plan_text": {"type": "string", "description": "Full plan text produced by the planner agent."},
        "base_sha": {
            "type": "string",
            "description": (
                "The 40-char hex target-branch HEAD the plan was authored against, recorded on the "
                "PlanArtifact so a plan bound to a stale base is detectable (SELFCATCH-3)."
            ),
        },
        "adequacy": {
            "type": "object",
            "description": (
                "The plan's four-section adequacy manifest recorded on the PlanArtifact (SELFCATCH-3). "
                "Each section is substantive (`content`) OR carries an explicit reasoned negative "
                "(`none_reason`); silence never passes."
            ),
            "properties": {
                "design": _ADEQUACY_SECTION_SCHEMA,
                "integration_seams": _ADEQUACY_SECTION_SCHEMA,
                "edge_cases": _ADEQUACY_SECTION_SCHEMA,
                "test_strategy": _ADEQUACY_SECTION_SCHEMA,
                "mechanism_placement": {
                    "type": "object",
                    "description": (
                        "A directive-linked ticket's fifth section — the generic-shape decision checked "
                        "against the ratified MechanismSketch."
                    ),
                },
                "approved_debt": {
                    "type": "array",
                    "description": "Audited debt waivers the plan explicitly approves (pattern + reason).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "pattern": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                    },
                },
            },
        },
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
                "reviewed_sha": {
                    "type": "string",
                    "description": (
                        "Full 40-char hex SHA the review bound to. REQUIRED: an undisclosed head is "
                        "refused, never read as agreement with the dispatch head (#4168)."
                    ),
                },
                "reviewer_identity": {"type": "string"},
                "gh_verify_result": {"type": "string", "enum": ["green", "pending", "failed"]},
                "blast_class": {"type": "string", "enum": ["substrate", "logic", "docs"]},
                "findings": {
                    "type": "array",
                    "description": ENVELOPE_FINDINGS_RULE,
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
            "required": ["verdict", "reviewed_sha"],
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
        "triage_recommendations": {
            "type": "array",
            "description": "Assessed needs-triage issues a shell-denied assessor hands back for approval.",
            "items": {
                "type": "object",
                "properties": {
                    "issue_url": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["keep", "close", "needs_info"]},
                    "suggested_labels": {"type": "array", "items": {"type": "string"}},
                    "priority": {"type": "string"},
                    "duplicate_of": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["issue_url", "verdict"],
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
#: - ``reviewing`` / ``codex_reviewing`` / ``codex_adversarial_reviewing``: a
#:   typed ``review_verdict`` returned for server-side recording (corr-11),
#:   carrying a verdict the recorder can persist. The three share one contract
#:   because they share one deliverable — a recorded verdict — so a codex review
#:   can no longer complete on prose while recording nothing.
#:   ``decisions`` used to satisfy this too, and that alternative is what let
#:   138 reviewing tasks complete having recorded no verdict at all while every
#:   open PR logged ``solo_overlay_no_review`` (#3654): a summary-plus-decisions
#:   result is indistinguishable from a healthy review, so the absence was
#:   invisible. The verdict is the only artifact the merge gate consumes, so it
#:   is the only accepted evidence.
#: - ``shipping``: at least one command executed (``git push``, ``gh pr``...).
#: - ``scanning_news``: at least one ``article_suggestion`` returned — the
#:   shell-denied scanner hands its candidates back through the envelope, so a
#:   summary-only run is a silently-dropped scan (#9), refused here.
#: - ``answering``: an ``answer`` draft returned — same shell-denied hand-back;
#:   a summary-only run dropped the drafted reply.
#:
#: Phases not in this map carry no evidence requirement of their own.
#: ``scoping`` and ``retro`` are intentionally lightweight and may complete on
#: prose alone (:data:`PROSE_SUMMARY_ACCEPTED_PHASES`); every OTHER absent phase
#: — ``debugging``, ``bughunt``, ``e2e``, ``e2e_reviewing``, ``requesting_review``,
#: ``architectural_review``, ``backlog_sweep``, ``dogfood_smoke``, ``eval_local``,
#: the ``codex_*`` review variants, and any free-form phase — must still RETURN a
#: result envelope, which :meth:`ProseSummaryPolicy.allowed` enforces.
PHASE_REQUIRED_EVIDENCE: dict[str, tuple[str, ...]] = {
    "planning": ("plan_text",),
    "coding": ("files_modified",),
    "testing": ("tests_run", "tests_passed"),
    "reviewing": ("review_verdict",),
    "codex_reviewing": ("review_verdict",),
    "codex_adversarial_reviewing": ("review_verdict",),
    "critic_reviewing": ("critic_verdict",),
    "directive_interpreting": ("directive_interpretation",),
    "directive_reading": ("directive_candidate",),
    "shipping": ("commands_executed",),
    "scanning_news": ("article_suggestions",),
    "triage_assessing": ("triage_recommendations",),
    "answering": ("answer",),
}


def required_evidence_for_phase(phase: str) -> tuple[str, ...]:
    """Return the accepted evidence fields for ``phase`` (empty if none required)."""
    return PHASE_REQUIRED_EVIDENCE.get(normalize_phase(phase), ())


#: Phases whose no-envelope run may still record the prose-only ``{"summary":
#: ...}`` fallback because the prose IS the deliverable — exactly the two the
#: :data:`PHASE_REQUIRED_EVIDENCE` block calls intentionally lightweight. Data,
#: not harness logic: widen this table — never the runner — to exempt a new phase.
PROSE_SUMMARY_ACCEPTED_PHASES: frozenset[str] = frozenset({"scoping", "retro"})


class ProseSummaryPolicy:
    """Phase predicates gating the envelope-less prose-only ``{"summary": ...}`` fallback."""

    @staticmethod
    def accepted(phase: str) -> bool:
        """Whether ``phase`` may record a prose summary when the agent emits no JSON envelope."""
        return normalize_phase(phase) in PROSE_SUMMARY_ACCEPTED_PHASES

    @staticmethod
    def allowed(phase: str) -> bool:
        """Whether ``_record_success`` may hand an envelope-less run on *phase* to the recorder."""
        return ProseSummaryPolicy.accepted(phase) or normalize_phase(phase) in PHASE_REQUIRED_EVIDENCE


type AgentResultBlob = dict[str, object]


def suggestion_url(item: object) -> str:
    """The persistable source URL of one article suggestion, or ``""`` if absent."""
    if not isinstance(item, dict):
        return ""
    return str(cast("ArticleSuggestion", item).get("url") or "").strip()


def answer_text(answer: object) -> str:
    """The persistable reply text of an answer envelope, or ``""`` if absent."""
    if not isinstance(answer, dict):
        return ""
    return str(cast("AnswerEnvelope", answer).get("text") or "").strip()


def recommendation_issue_url(item: object) -> str:
    """The persistable issue URL of one triage recommendation, or ``""`` if absent."""
    if not isinstance(item, dict):
        return ""
    return str(cast("TriageRecommendation", item).get("issue_url") or "").strip()


def recommendation_persists(item: object) -> bool:
    """Whether one triage recommendation carries what the recorder actually PERSISTS."""
    from teatree.core.models.pending_triage_recommendation import (  # noqa: PLC0415 — ORM/app-registry
        VALID_TRIAGE_VERDICTS,
    )

    if not recommendation_issue_url(item):
        return False
    verdict = str(cast("TriageRecommendation", item).get("verdict") or "").strip().lower()
    return verdict in VALID_TRIAGE_VERDICTS


def candidate_carries_payload(envelope: object) -> bool:
    """Whether a directive-candidate envelope carries something the recorder persists (#116)."""
    if not isinstance(envelope, dict):
        return False
    typed = cast("DirectiveCandidateEnvelope", envelope)
    return typed.get("is_directive") is True and bool(str(typed.get("normalized_constraint") or "").strip())


def interpretation_carries_payload(envelope: object) -> bool:
    """Whether a directive-interpretation envelope carries something the recorder persists."""
    if not isinstance(envelope, dict):
        return False
    typed = cast("DirectiveInterpretationEnvelope", envelope)
    sketch = typed.get("sketch")
    if isinstance(sketch, dict) and sketch:
        return True
    questions = typed.get("clarifying_questions")
    return isinstance(questions, list) and any(str(q).strip() for q in questions)


def verdict_carries_payload(envelope: object) -> bool:
    """Whether a review-verdict envelope names a verdict the recorder can persist (#3654)."""
    from teatree.core.models.review_verdict import ReviewVerdict  # noqa: PLC0415 — deferred: ORM/app-registry

    if not isinstance(envelope, dict):
        return False
    verdict = str(cast("ReviewVerdictEnvelope", envelope).get("verdict") or "").strip().lower()
    return verdict in {choice.value for choice in ReviewVerdict.Verdict}


#: Channels whose "evidence present" test is stricter than coarse truthiness:
#: the field must carry what the recorder actually PERSISTS (a url-bearing
#: suggestion, a text-bearing answer). Without this a schema-violating-but-
#: nonempty hand-back (``[{"title": "x"}]`` / ``{"thread_ref": "x"}``) the
#: recorder drops entirely would pass the gate and COMPLETE the task over zero
#: persisted work — the exact silent-drop class #9 closes.
_FIELD_PERSISTS: dict[str, Callable[[object], bool]] = {
    "article_suggestions": lambda v: isinstance(v, list) and any(suggestion_url(item) for item in v),
    "triage_recommendations": lambda v: isinstance(v, list) and any(recommendation_persists(item) for item in v),
    "answer": lambda v: bool(answer_text(v)),
    "directive_interpretation": interpretation_carries_payload,
    "directive_candidate": candidate_carries_payload,
    "review_verdict": verdict_carries_payload,
}


def _field_carries_evidence(result: AgentResultBlob, field: str) -> bool:
    predicate = _FIELD_PERSISTS.get(field)
    if predicate is not None:
        return predicate(result.get(field))
    return bool(result.get(field))


def check_evidence(result: AgentResultBlob, phase: str) -> str:
    """Return an error message if *result* lacks required evidence, else ``""``."""
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
