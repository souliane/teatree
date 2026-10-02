"""The candidate-DERIVED anti-vacuity teeth check for synthesized evals (#2447).

A synthesized ``under_load`` spec is proven non-vacuous by grading its matchers
against transcripts seeded FROM THE CANDIDATE — the cited drift's own tool-call
shape, and the compliant shape — NOT against ``promote``'s FIXED session.py-edit /
Task-delegate transcripts. Grading against ``promote``'s fixed transcripts would
ACCEPT a spec whose matchers are unrelated to the candidate's own drift (a
mislabeled scenario) and REJECT a correctly-targeted one; seeding the transcripts
from the candidate proves the matchers reject the SPECIFIC drift the candidate
cites. The grader is the SAME :func:`teatree.eval.report.evaluate` the suite runs,
so a spec that clears this check is graded identically once it lands.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypedDict

from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.report import evaluate
from teatree.eval.transcript import extract_terminal_reason, extract_text_blocks, extract_tool_calls, parse_stream_json


class ToolCallShape(TypedDict):
    """One tool call the teeth check seeds a transcript from: a ``name`` + ``input``.

    The candidate's OWN drift action (``fail``) and compliant action (``pass``)
    expressed in the shape the stream-json ``tool_use`` block carries, so the grader
    sees exactly the action the candidate cites. ``input`` is an arbitrary tool-args
    JSON object, typed ``dict[str, Any]`` to mirror
    :class:`teatree.eval.models.EvalToolCall.input`.
    """

    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TeethCheckResult:
    """Whether the synthesized matchers reject the candidate's OWN cited drift."""

    can_fail: bool
    reason: str


def _tool_call_transcript(scenario_name: str, suffix: str, tool_call: ToolCallShape) -> str:
    """A minimal stream-json transcript exercising one *tool_call* shape.

    Seeds the teeth-check ``_fail`` / ``_pass`` runs from the candidate's OWN drift
    (and compliant) tool-call, so the grader sees exactly the action the candidate
    cites — not ``promote``'s fixed session.py-edit shape. Uses the same stream-json
    events the live runners feed the grader.
    """
    return "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init", "session_id": f"derive-{scenario_name}-{suffix}"}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": f"toolu_{suffix}",
                                "name": tool_call["name"],
                                "input": dict(tool_call["input"]),
                            }
                        ],
                    },
                }
            ),
            json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1}),
        ]
    )


def _run_from_transcript(spec_name: str, raw: str) -> EvalRun:
    """Parse a stream-json transcript string into an :class:`EvalRun` for grading.

    Reuses the SAME extractors the live runners feed the grader, so the teeth check
    grades a transcript byte-for-byte the way the suite will once the scenario
    lands — no parallel grading path that could drift from production.
    """
    events = parse_stream_json(raw)
    terminal_reason, is_error = extract_terminal_reason(events)
    return EvalRun(
        spec_name=spec_name,
        tool_calls=tuple(extract_tool_calls(events)),
        text_blocks=tuple(extract_text_blocks(events)),
        terminal_reason=terminal_reason,
        is_error=is_error,
        raw_stdout=raw,
        raw_stderr="",
    )


def teeth_check_against_candidate(
    spec: EvalSpec, *, fail_tool_call: Mapping[str, object], pass_tool_call: Mapping[str, object]
) -> TeethCheckResult:
    """Prove the synthesized matchers reject the candidate's OWN cited drift.

    Seeds a ``_fail`` transcript from the candidate's drift tool-call shape
    (*fail_tool_call*) and a ``_pass`` from the compliant shape (*pass_tool_call*),
    then runs the REAL grader (:func:`evaluate`) against both:

    *   the ``_fail`` run (the candidate's cited drift) MUST grade FAIL — else the
        matchers do not reject the drift the candidate actually describes and the
        spec is vacuous against its own candidate;
    *   the ``_pass`` run (the compliant shape) MUST grade PASS — else the scenario
        is a tautology that rejects even compliant behaviour.

    Unlike a check against ``promote``'s fixed session.py-edit transcripts, this
    cannot ACCEPT a mislabeled spec (matchers unrelated to the cited drift) or
    REJECT a correctly-targeted one — the transcripts ARE the candidate's drift.
    """
    fail_shape = _coerce_shape(fail_tool_call)
    pass_shape = _coerce_shape(pass_tool_call)
    if not fail_shape["name"] or not pass_shape["name"]:
        return TeethCheckResult(
            can_fail=False,
            reason="synthesizer supplied no candidate drift/compliant tool-call shapes to teeth-check against",
        )
    fail_run = _run_from_transcript(spec.name, _tool_call_transcript(spec.name, "fail", fail_shape))
    if evaluate(spec, fail_run).passed:
        return TeethCheckResult(
            can_fail=False,
            reason="grader did NOT reject the candidate's own cited drift transcript — matchers are vacuous",
        )
    pass_run = _run_from_transcript(spec.name, _tool_call_transcript(spec.name, "pass", pass_shape))
    if not evaluate(spec, pass_run).passed:
        return TeethCheckResult(
            can_fail=False,
            reason="grader rejected the candidate's compliant transcript — scenario is a tautology",
        )
    return TeethCheckResult(
        can_fail=True,
        reason="grader proven to FAIL the candidate's own cited drift and PASS its compliant shape",
    )


def _coerce_shape(tool_call: Mapping[str, object]) -> ToolCallShape:
    """Normalize a loose tool-call mapping into a typed :class:`ToolCallShape`."""
    tool_input = tool_call.get("input")
    return ToolCallShape(
        name=str(tool_call.get("name") or ""),
        input=dict(tool_input) if isinstance(tool_input, Mapping) else {},
    )


__all__ = ["TeethCheckResult", "ToolCallShape", "teeth_check_against_candidate"]
