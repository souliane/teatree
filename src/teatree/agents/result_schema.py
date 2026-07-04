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

from typing import TypedDict

from teatree.core.modelkit.phases import normalize_phase


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


class ReviewVerdictEnvelope(TypedDict, total=False):
    """A reviewing-phase agent's typed verdict, recorded server-side (corr-11).

    A headless reviewing phase is denied the shell (PR-11), so it cannot run
    ``t3 review record``. It RETURNS this instead: the orchestrator
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
#:
#: Phases not in this map (``scoping``, ``retro``) carry no evidence
#: requirement — they are intentionally lightweight.
PHASE_REQUIRED_EVIDENCE: dict[str, tuple[str, ...]] = {
    "planning": ("plan_text",),
    "coding": ("files_modified",),
    "testing": ("tests_run", "tests_passed"),
    "reviewing": ("decisions", "review_verdict"),
    "shipping": ("commands_executed",),
}


def required_evidence_for_phase(phase: str) -> tuple[str, ...]:
    """Return the accepted evidence fields for ``phase`` (empty if none required)."""
    return PHASE_REQUIRED_EVIDENCE.get(normalize_phase(phase), ())


type AgentResultBlob = dict[str, object]


def check_evidence(result: AgentResultBlob, phase: str) -> str:
    """Return an error message if *result* lacks required evidence, else ``""``.

    A field is "present" iff the result has the key AND its value is
    truthy (non-zero int, non-empty list/dict/string). Supplying ANY of the
    acceptable fields for ``phase`` satisfies the check — the requirement
    is "one of these, non-empty", not "all of these".

    Sub-agent contracts that opt out of normal completion (``needs_user_input``
    handoffs) bypass the check: the agent is *not* claiming the phase is
    done, so demanding phase evidence would be incoherent.
    """
    if result.get("needs_user_input"):
        return ""
    accepted = required_evidence_for_phase(phase)
    if not accepted:
        return ""
    if any(result.get(field) for field in accepted):
        return ""
    joined = " | ".join(accepted)
    return (
        f"missing required evidence for phase '{phase}': result must include one of [{joined}] "
        f"with a non-empty value (codex #1282-6)"
    )
