"""The model-agnostic result-envelope contract every headless brief teaches (#3660).

The recorder refuses a run whose final output carries no JSON result envelope
(``no_result_envelope``), and that refusal is correct — but the brief only ever
gestured at the envelope in a trailing "if /t3:next is not available" fallback,
which reads as optional to any model that was not the one the prompts were
written against. On the metered router lane every headless task failed that way:
real inference, real reasoning, prose result, zero recorded work.

So the contract is stated here once, in full, and injected into EVERY phase's
brief: the allowed keys (derived from :data:`RESULT_JSON_SCHEMA`, never a second
hand-maintained list), the phase's required evidence field (derived from
:data:`PHASE_REQUIRED_EVIDENCE`), and a literal minimal example the model can
copy. This is the instruction half of the fix; schema enforcement on the binding
would be strictly stronger, and the harness reports ``structured_output=False``
where it is unreachable.
"""

import json
from collections.abc import Mapping

from teatree.agents.result_schema import (
    RESULT_JSON_SCHEMA,
    AgentResult,
    conditional_evidence_for_phase,
    required_evidence_for_phase,
)
from teatree.agents.result_schema_draft import EXECUTOR_DRAFT_FIELDS
from teatree.agents.result_schema_owner_stop import owner_stop_limits_lines
from teatree.core.modelkit.owner_decision import OWNER_RAISABLE_DECISIONS
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.modelkit.review_contract import VERDICT_CHECKS_RULE
from teatree.core.models.reviewer_identity import REVIEWER_IDENTITY_INSTRUCTION

CONTRACT_HEADING = "# Result Envelope — REQUIRED OUTPUT CONTRACT"

_FIX_RECORD_FIELD_LINES: tuple[str, ...] = (
    "",
    "THIS IS A FIX TICKET — RETURN ITS FixRecord IN THE ENVELOPE (the Definition-of-Done gate",
    "refuses DELIVERED without one, and nothing but this envelope writes it):",
    '  "fix_record": {"root_cause": "<the DEEPEST cause, not the symptom you patched>",',
    '                 "evidence": "<why that is the cause — what you read/ran that shows it>",',
    '                 "regression_test": "<path::node of the RED-first test that pins it>",',
    '                 "observed_red": "<attestation you saw that test FAIL against the pre-fix code>",',
    '                 "recurrence_fingerprint": "<stable signature, e.g. module:condition>"}',
    "Every field is required and non-blank — a PARTIAL object is REFUSED and records nothing,",
    "so omit the key entirely rather than guessing at a field you cannot honestly fill.",
    "A merged manifestation patch with no stated root cause is not done.",
)
FIX_RECORD_NO_RED_FIELD_LINES: tuple[str, ...] = tuple(
    line.replace("RED-first ", "") for line in _FIX_RECORD_FIELD_LINES if '"observed_red"' not in line
)
FIX_RECORD_RETURN_LINES: tuple[str, ...] = (
    *_FIX_RECORD_FIELD_LINES,
    (
        "Before editing, run `repro record-red` on the pre-fix HEAD; coding completion requires it "
        "and testing records GREEN automatically."
    ),
)
FIX_RECORD_REVIEW_LINES: tuple[str, ...] = (
    *_FIX_RECORD_FIELD_LINES,
    "Return it only with merge_safe; observed_red is your own revert-to-RED proof.",
)
FIX_RECORD_REVIEW_NO_RED_LINES: tuple[str, ...] = (*FIX_RECORD_NO_RED_FIELD_LINES, "Return it only with merge_safe.")

#: One minimal, schema-valid envelope fragment per evidence field, so the example
#: the brief shows is the exact shape the recorder accepts rather than a paraphrase
#: of it. Keyed on every field named in ``PHASE_REQUIRED_EVIDENCE``; a phase whose
#: field is absent here degrades to a summary-only example (still a valid envelope).
_EVIDENCE_EXAMPLES: Mapping[str, AgentResult] = {
    "plan_text": {"plan_text": "1. Add X to module Y. 2. Wire the call site. 3. Test Z."},
    "files_modified": {"files_modified": [{"path": "src/teatree/agents/prompt.py", "action": "modified"}]},
    "tests_run": {"tests_run": [{"name": "tests/teatree_agents/test_prompt.py::test_contract", "passed": True}]},
    "decisions": {"decisions": ["Kept the failure loud rather than synthesising an envelope."]},
    "review_verdict": {
        "review_verdict": {
            "verdict": "merge_safe",
            "reviewed_sha": "<the full 40-char head sha you were dispatched for>",
            "reviewer_identity": REVIEWER_IDENTITY_INSTRUCTION,
            "gh_verify_result": "green",
            "blast_class": "logic",
            # Non-empty on a PASS: the example a reviewer copies outranks the prose
            # beside it (``modelkit.review_contract.ENVELOPE_FINDINGS_RULE``).
            "findings": [{"severity": "low", "summary": "<what you observed>", "file": "src/x.py", "line": 42}],
            # EVERY criterion, or the verdict is refused and records nothing — same
            # reasoning as ``findings`` above: the example a reviewer copies outranks
            # the prose beside it.
            "rubric_grades": [
                {
                    "ordinal": 0,
                    "text_hash": "<hash from brief>",
                    "status": "pass",
                    "rationale": "<the test that proves it>",
                }
            ],
        }
    },
    "critic_verdict": {
        "critic_verdict": {
            "grader_identity": "<your grader id>",
            "items": [{"slug": "<rubric-item>", "status": "pass", "citation": "<file:line or command output>"}],
        }
    },
    "directive_interpretation": {
        "directive_interpretation": {
            "interpreter_identity": "<your interpreter id>",
            "constraint_statement": "<the directive as one enforceable constraint>",
            "sketch": {"kind": "default_behaviour", "setting_key": "", "policy_chokepoint": "<module.function>"},
        }
    },
    "commands_executed": {"commands_executed": ["git push -u origin HEAD", "gh pr create --base main"]},
    "article_suggestions": {
        "article_suggestions": [
            {"title": "<article title>", "url": "https://example.com/article", "rationale": "<why it matters>"}
        ]
    },
    "triage_recommendations": {
        "triage_recommendations": [
            {"issue_url": "https://github.com/<owner>/<repo>/issues/1", "verdict": "keep", "rationale": "<why>"}
        ]
    },
    "answer": {"answer": {"text": "<the drafted reply, in the user's voice>", "thread_ref": "<thread ts, or ''>"}},
    "review_request": {
        "review_request": {
            "ready": True,
            "pr_url": "<a PR URL from the task message>",
            "title": "<type(scope): subject>",
        }
    },
    "ship_readiness": {"ship_readiness": {"ready": True, "reasons": []}},
    # The id `ticket sweep-begin` printed — the count is read from the run's own
    # recorded URLs, so the example deliberately shows no changed_count to restate.
    "ticket_sweep": {"ticket_sweep": {"run_id": "<the id `t3 <overlay> ticket sweep-begin` printed>"}},
}


def allowed_keys() -> tuple[str, ...]:
    """Every key the envelope schema permits — ``additionalProperties`` is false."""
    properties = RESULT_JSON_SCHEMA.get("properties")
    return tuple(str(key) for key in properties) if isinstance(properties, Mapping) else ()


def required_keys_phrase(phase: str) -> str:
    """The phase's required envelope keys, rendered for an instruction line."""
    required = required_evidence_for_phase(phase)
    if not required:
        return "`summary`"
    phrase = "`summary` and " + " or ".join(f"`{field}`" for field in required)
    conditional = conditional_evidence_for_phase(phase)
    if conditional:
        also = " and ".join(f"`{field}`" for field in conditional)
        phrase += f" (plus {also} when the request implies work)"
    return phrase


def corrective_instruction(phase: str) -> str:
    return (
        f"your last run omitted the required trailing JSON result envelope "
        f"({required_keys_phrase(phase)}) — emit it as the last thing you write, "
        "plain JSON, nothing after it."
    )


def refused_envelope_instruction(refusal: str) -> str:
    return (
        f"your last run's result envelope was refused ({refusal}) — re-emit the complete envelope "
        "with exactly those keys corrected, as the last thing you write, plain JSON, nothing after it."
    )


def _taught_keys(*, read_only_venue: bool) -> tuple[str, ...]:
    """The keys a brief lists: a worker brief omits the draft keys only the read-only executor returns."""
    draft_keys = set(EXECUTOR_DRAFT_FIELDS.values())
    return tuple(key for key in allowed_keys() if read_only_venue or key not in draft_keys)


def _with_reviewer_identity(evidence: AgentResult, identity: str) -> AgentResult:
    """*evidence* carrying the dispatch's assigned identity — a COPY, never the constant."""
    verdict = evidence.get("review_verdict")
    if not identity or not isinstance(verdict, Mapping):
        return evidence
    return {**evidence, "review_verdict": {**verdict, "reviewer_identity": identity}}


def _evidence_fields(phase: str, *, read_only_venue: bool) -> tuple[str, ...]:
    """The evidence a run must return; on the read-only executor a draft phase returns its draft, not a command log."""
    field = EXECUTOR_DRAFT_FIELDS.get(normalize_phase(phase)) if read_only_venue else None
    return (field,) if field else required_evidence_for_phase(phase)


def envelope_example(phase: str, *, reviewer_identity: str = "", read_only_venue: bool = False) -> AgentResult:
    """A minimal envelope for *phase* that satisfies the phase evidence gate.

    *reviewer_identity* replaces the self-naming template with the dispatch's assigned
    literal: a model copies the example over the prose beside it, so the example is where
    the identity has to be concrete (#2663).
    """
    example: AgentResult = {"summary": "<one line: what you did and how you proved it>"}
    for field in _evidence_fields(phase, read_only_venue=read_only_venue):
        evidence = _EVIDENCE_EXAMPLES.get(field)
        if evidence is not None:
            example.update(_with_reviewer_identity(evidence, reviewer_identity))
            break
    example["needs_user_input"] = False
    return example


def _evidence_lines(phase: str, *, read_only_venue: bool) -> tuple[str, ...]:
    """The phase-scoped "this key is also mandatory" clause, or the summary-only note."""
    required = _evidence_fields(phase, read_only_venue=read_only_venue)
    if not required:
        return (f"- Phase `{normalize_phase(phase) or phase}` requires no extra evidence key beyond `summary`.",)
    fields = " or ".join(f"`{field}`" for field in required)
    lines = [
        f"- Phase `{normalize_phase(phase)}` ALSO requires a non-empty {fields}.",
        "  A result without it is refused as missing evidence and the whole run is wasted.",
    ]
    conditional = conditional_evidence_for_phase(phase)
    if conditional:
        also = " and ".join(f"`{field}`" for field in conditional)
        lines.append(f"  When this task's own request implies work, {also} is required TOO.")
    return tuple(lines)


def _verdict_consistency_lines(phase: str) -> tuple[str, ...]:
    """The checks-vs-verdict consistency clause, on the phases that return a verdict.

    Only those phases: a coding brief that recites a reviewer rule spends context teaching
    a contract it can never breach. Keyed on the phase's own required evidence rather than
    on a second hand-listed set of review phases, so a phase that starts returning a
    verdict inherits the clause instead of quietly going without it.
    """
    if "review_verdict" not in required_evidence_for_phase(phase):
        return ()
    return ("", f"- {VERDICT_CHECKS_RULE}")


def final_output_reminder_line(phase: str, *, read_only_venue: bool = False) -> str:
    """The one-line restatement of the contract, placed last in the work prompt.

    The full contract sits in the system context; recency is what a non-Claude
    model actually acts on, so the work prompt closes by naming the phase's own
    required key again rather than trusting the system prompt to carry it alone.
    """
    required = _evidence_fields(phase, read_only_venue=read_only_venue)
    keys = "`summary`" + ("" if not required else " and " + " or ".join(f"`{field}`" for field in required))
    return (
        f"6. FINAL OUTPUT: end with the JSON result envelope ({keys}) as the last thing you write — "
        "prose with no envelope is refused and the run is discarded."
    )


def envelope_contract_lines(
    phase: str, *, reviewer_identity: str = "", read_only_venue: bool = False
) -> tuple[str, ...]:
    """The full envelope contract taught to a headless brief for *phase*.

    Every phase gets it, verbatim: assuming the model already knows the format is
    what let prose leak through on a lane whose model never saw these prompts.
    """
    return (
        "",
        CONTRACT_HEADING,
        "",
        "Only the final JSON envelope counts; prose or a synthesized envelope is refused",
        "(`no_result_envelope`) and discarded; no partial credit.",
        "",
        "- End with exactly one plain JSON object; no code fence or trailing text.",
        "- `summary` (string) is required on every phase.",
        "- Owner stop (`needs_user_input`: true): `user_input_reason` (one question ending ?), `user_input_kind`,",
        (
            '  `user_input_checked`, `user_input_card` {"why", "blocker", '
            '"options": [{"label", "description", "recommended"}]}; stop.'
        ),
        f"  {', '.join(OWNER_RAISABLE_DECISIONS)}; an access or permission grant is credentials.",
        *(f"  {line}" for line in owner_stop_limits_lines()),
        "  Decide everything else yourself.",
        (
            "- For each required skill used, add `skill_application`: "
            '`{"skill": "name", "evidence": "short concrete reference"}`; omissions are unverified.'
        ),
        *_evidence_lines(phase, read_only_venue=read_only_venue),
        *_verdict_consistency_lines(phase),
        "- Only these keys are allowed:",
        f"  {', '.join(_taught_keys(read_only_venue=read_only_venue))}",
        "",
        "Minimal valid envelope for this phase — copy this shape exactly:",
        json.dumps(
            envelope_example(phase, reviewer_identity=reviewer_identity, read_only_venue=read_only_venue), indent=1
        ),
    )
