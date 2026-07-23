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

from teatree.agents.result_schema import RESULT_JSON_SCHEMA, AgentResult, required_evidence_for_phase
from teatree.core.modelkit.phases import normalize_phase

CONTRACT_HEADING = "# Result Envelope — REQUIRED OUTPUT CONTRACT"

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
            "reviewed_sha": "0" * 40,
            "reviewer_identity": "<your reviewer id>",
            "gh_verify_result": "green",
            "blast_class": "logic",
            "findings": [],
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
            "sketch": {"kind": "config_setting", "setting_key": "<setting>", "policy_chokepoint": "<module.function>"},
        }
    },
    "directive_candidate": {
        "directive_candidate": {
            "reader_identity": "<your reader id>",
            "is_directive": True,
            "normalized_constraint": "<the constraint in one sentence>",
            "cited_signal": "<the text you read it from>",
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
}


def allowed_keys() -> tuple[str, ...]:
    """Every key the envelope schema permits — ``additionalProperties`` is false."""
    properties = RESULT_JSON_SCHEMA.get("properties")
    return tuple(str(key) for key in properties) if isinstance(properties, Mapping) else ()


def envelope_example(phase: str) -> AgentResult:
    """A minimal envelope for *phase* that satisfies the phase evidence gate."""
    example: AgentResult = {"summary": "<one line: what you did and how you proved it>"}
    for field in required_evidence_for_phase(phase):
        evidence = _EVIDENCE_EXAMPLES.get(field)
        if evidence is not None:
            example.update(evidence)
            break
    example["needs_user_input"] = False
    return example


def _evidence_lines(phase: str) -> tuple[str, ...]:
    """The phase-scoped "this key is also mandatory" clause, or the summary-only note."""
    required = required_evidence_for_phase(phase)
    if not required:
        return (f"- Phase `{normalize_phase(phase) or phase}` requires no extra evidence key beyond `summary`.",)
    fields = " or ".join(f"`{field}`" for field in required)
    return (
        f"- Phase `{normalize_phase(phase)}` ALSO requires a non-empty {fields}.",
        "  A result without it is refused as missing evidence and the whole run is wasted.",
    )


def final_output_reminder_line(phase: str) -> str:
    """The one-line restatement of the contract, placed last in the work prompt.

    The full contract sits in the system context; recency is what a non-Claude
    model actually acts on, so the work prompt closes by naming the phase's own
    required key again rather than trusting the system prompt to carry it alone.
    """
    required = required_evidence_for_phase(phase)
    keys = "`summary`" + ("" if not required else " and " + " or ".join(f"`{field}`" for field in required))
    return (
        f"6. FINAL OUTPUT: end with the JSON result envelope ({keys}) as the last thing you write — "
        "prose with no envelope is refused and the run is discarded."
    )


def envelope_contract_lines(phase: str) -> tuple[str, ...]:
    """The full envelope contract taught to a headless brief for *phase*.

    Every phase gets it, verbatim: assuming the model already knows the format is
    what let prose leak through on a lane whose model never saw these prompts.
    """
    return (
        "",
        CONTRACT_HEADING,
        "",
        "Your work is recorded ONLY if your final output contains the result envelope.",
        "Prose with no envelope is refused (`no_result_envelope`) and the entire run is",
        "discarded — there is no partial credit and no envelope is ever synthesised for you.",
        "",
        "- Emit exactly ONE JSON object as the very last thing you write. Nothing after it.",
        "- Plain JSON — no markdown code fence, no trailing commentary, no explanation.",
        "- `summary` (string) is required on every phase.",
        "- `needs_user_input` is a boolean; when true, also set `user_input_reason` (string)",
        "  and stop rather than guessing.",
        *_evidence_lines(phase),
        "- Use ONLY these keys — any other key is rejected outright:",
        f"  {', '.join(allowed_keys())}",
        "",
        "Minimal valid envelope for this phase — copy this shape exactly:",
        json.dumps(envelope_example(phase), indent=2),
    )
