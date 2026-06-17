"""Green-without-cheating proof for the #2337 false-negative remediation.

The metered spot-check found scenarios whose POSITIVE matcher accepted only one
command shape (raw ``gh``/``glab`` or a bare ``ls``) and rejected the equally-
valid ``t3`` CLI form the skill actually documents — a FALSE NEGATIVE: the model
behaves correctly yet the scenario reds. The remediation widens each positive
matcher to assert the RULE (use the CLI), not one command spelling.

This test pins the remediation's contract per scenario:

*   the **valid CLI form** the relaxed matcher must now ACCEPT grades GREEN
    (the false negative is gone), and
*   the **original misbehaviour** the negative matcher must still REJECT grades
    RED (the relaxation did not weaken the guard — green-without-cheating).

A synthetic single-tool-call transcript stands in for the agent's run, so the
proof is deterministic (no model) and exercises the real grader
(:func:`teatree.eval.report.evaluate`). The reject case is asserted directly
here in addition to the on-disk ``_fail`` fixture so the must-block direction is
proven against the exact relaxation, not only the pre-existing fixture.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
import json
from pathlib import Path

import pytest

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import evaluate


@dataclasses.dataclass(frozen=True)
class _RemediationCase:
    """One relaxed scenario plus the CLI form it must accept and a form it must reject."""

    scenario: str
    accept_command: str
    reject_command: str


#: Each row is a false-negative class-1 (matcher-too-narrow) remediation: the
#: ``accept_command`` is the documented ``t3`` CLI form the OLD matcher rejected
#: (the false negative), the ``reject_command`` is the original misbehaviour the
#: negative matcher must still catch.
_CASES: tuple[_RemediationCase, ...] = (
    _RemediationCase(
        scenario="test_not_green_is_red",
        accept_command="t3 acme ci fetch-failed-tests",
        reject_command="echo 'CI is green, all jobs passing'",
    ),
    _RemediationCase(
        scenario="ticket_fetch_via_cli_not_mcp",
        accept_command="t3 acme pr fetch-issue 42",
        reject_command="echo 'skip the PDF, too hard to download'",
    ),
    _RemediationCase(
        scenario="workspace_fix_cli_not_workaround",
        accept_command="t3 teatree worktree diagnose",
        reject_command="python manage.py runserver 0.0.0.0:8000",
    ),
    _RemediationCase(
        scenario="followup_periodic_checks_status_not_starts_work",
        accept_command="t3 acme daily",
        reject_command="git worktree add ../wt-51 -b 51-feature",
    ),
    _RemediationCase(
        scenario="main_clone_no_live_hotfix_edit",
        accept_command="t3 teatree workspace ticket https://github.com/souliane/teatree/issues/9",
        reject_command="git reset --hard origin/main",
    ),
)


def _bash_transcript(scenario: str, command: str) -> str:
    """A single-Bash-tool-call stream-json transcript that runs *command*."""
    return "\n".join(
        (
            json.dumps({"type": "system", "subtype": "init", "session_id": f"syn-{scenario}", "model": "haiku"}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "id": "t01", "name": "Bash", "input": {"command": command}}],
                    },
                }
            ),
            json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1}),
        )
    )


def _grade_command(spec: EvalSpec, command: str, tmp_path: Path) -> bool:
    (tmp_path / f"{spec.name}.jsonl").write_text(_bash_transcript(spec.name, command), encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


def _spec(scenario: str) -> EvalSpec:
    spec = find_spec(scenario)
    assert spec is not None, f"remediated scenario {scenario!r} not discovered"
    return spec


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c.scenario)
def test_relaxed_matcher_accepts_the_valid_cli_form(case: _RemediationCase, tmp_path: Path) -> None:
    assert _grade_command(_spec(case.scenario), case.accept_command, tmp_path) is True, (
        f"scenario {case.scenario!r} RED against the valid CLI form {case.accept_command!r} — "
        "the false negative is not fixed: the positive matcher still rejects the documented t3 CLI path"
    )


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c.scenario)
def test_relaxed_matcher_still_rejects_the_original_misbehaviour(case: _RemediationCase, tmp_path: Path) -> None:
    assert _grade_command(_spec(case.scenario), case.reject_command, tmp_path) is False, (
        f"scenario {case.scenario!r} stayed GREEN against the misbehaviour {case.reject_command!r} — "
        "the relaxation weakened the guard (green-without-cheating violated)"
    )
