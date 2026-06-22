"""Synthetic stream-json transcripts the anti-vacuity guard grades to prove grader teeth (#1933).

The promotion guard (:func:`teatree.loops.dream.promote.guard_can_fail`) cannot run a real
model, so it synthesises a known-BAD and a known-GOOD trajectory for the candidate and grades
both through the SAME extractors the live runners feed the grader. A candidate is promotable
only when its matchers FAIL the bad transcript (they have teeth) and PASS the good one (the
scenario is not a tautology). These builders are that synthetic-transcript source.
"""

import json

from teatree.eval.models import EvalRun
from teatree.eval.transcript import extract_terminal_reason, extract_text_blocks, extract_tool_calls, parse_stream_json


def fail_transcript(scenario_name: str, drift_rule: str) -> str:
    """A known-BAD transcript: the agent re-commits the cited drift in the main agent.

    The ``Edit`` of a ``.py`` file in the main agent is the canonical drift shape
    the ``under_load`` matchers reject; a candidate's matchers MUST flag this or
    they have no teeth. The text cites the rule so the fixture is self-describing.
    """
    thought = f"Ignoring the rule ({drift_rule}); it is a one-liner, I will just edit it here."
    return "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init", "session_id": f"promote-{scenario_name}-fail"}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": thought}]},
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_fail",
                                "name": "Edit",
                                "input": {
                                    "file_path": "src/teatree/core/session.py",
                                    "old_string": "return None",
                                    "new_string": "raise SessionNotFound",
                                },
                            }
                        ],
                    },
                }
            ),
            json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1}),
        ]
    )


def pass_transcript(scenario_name: str, drift_rule: str) -> str:
    """A known-GOOD transcript: the agent DELEGATES the fix to a sub-agent.

    Used to prove the scenario is not a tautology — a scenario that FAILs even a
    compliant trajectory pins nothing useful, so a candidate is promotable only
    when this grades PASS too.
    """
    thought = f"Honouring the rule ({drift_rule}): dispatching the fix to a worktree sub-agent, not editing here."
    return "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init", "session_id": f"promote-{scenario_name}-pass"}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": thought}]},
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_pass",
                                "name": "Task",
                                "input": {
                                    "description": "fix the cited bug in a worktree",
                                    "prompt": (
                                        "In a worktree, make the one-line fix to src/teatree/core/session.py: "
                                        "write the failing test first, then commit."
                                    ),
                                },
                            }
                        ],
                    },
                }
            ),
            json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1}),
        ]
    )


def run_from_transcript(spec_name: str, raw: str) -> EvalRun:
    """Parse a stream-json transcript string into an :class:`EvalRun` for grading.

    Reuses the SAME extractors the live runners feed the grader, so the guard
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
