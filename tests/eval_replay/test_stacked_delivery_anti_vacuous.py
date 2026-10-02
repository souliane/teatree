"""Anti-vacuity for the stacked-delivery ship evals.

Both scenarios shipped ``_pass``/``_fail`` fixtures but no replay test, so nothing
proved their matchers discriminate. These are free deterministic trip-wires: the
``_pass`` fixture must grade GREEN, the ``_fail`` fixture RED, and removing the
matchers must turn the ``_fail`` fixture GREEN -- the mandatory teeth check, since a
matcher whose removal does not change the verdict guards nothing.

``retarget_preserves_published_history`` is RED on the metered lane at the time of
writing (the agent merges the updated default and never pushes). These trip-wires are
what make that red attributable to behaviour rather than to a toothless or over-fit
matcher set.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by the
# established eval-suite convention.

import dataclasses
from pathlib import Path

import pytest

from teatree.eval.api_runner import load_agent_definition
from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall
from teatree.eval.report import evaluate

_RETARGET = "stacked_delivery_retarget_preserves_published_history"
_PARENT = "stacked_delivery_sets_repo_scoped_parent"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"


def _spec(name: str) -> EvalSpec:
    spec = find_spec(name)
    assert spec is not None, f"scenario {name!r} not discovered — check evals/scenarios/ship.yaml"
    return spec


def _grade(spec: EvalSpec, suffix: str, tmp_path: Path) -> bool:
    fixture = _FIXTURES / f"{spec.name}_{suffix}.stream.jsonl"
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


@pytest.mark.parametrize("name", [_RETARGET, _PARENT])
def test_pass_fixture_is_green(name: str, tmp_path: Path) -> None:
    assert _grade(_spec(name), "pass", tmp_path) is True, f"{name} RED against its _pass fixture — matchers over-fit"


@pytest.mark.parametrize("name", [_RETARGET, _PARENT])
def test_fail_fixture_is_red(name: str, tmp_path: Path) -> None:
    assert _grade(_spec(name), "fail", tmp_path) is False, f"{name} stayed GREEN against its _fail fixture — toothless"


@pytest.mark.parametrize("name", [_RETARGET, _PARENT])
def test_removing_matchers_turns_fail_green(name: str, tmp_path: Path) -> None:
    toothless = dataclasses.replace(_spec(name), matchers=())
    assert _grade(toothless, "fail", tmp_path) is True, (
        f"with the matchers removed {name}'s _fail fixture must go GREEN — else it fails for a "
        "reason unrelated to the matchers and the teeth proof is moot"
    )


def test_retarget_scenario_declares_a_git_fixture() -> None:
    assert _spec(_RETARGET).fixture == "git_repo", (
        "the retarget scenario's prompt presupposes a layer worktree, so it must provision one — "
        "without it every git runs in the neutral empty cwd and the failure is unattributable"
    )


def _verdict_for(spec: EvalSpec, command: str) -> str:
    run = EvalRun(
        spec_name=spec.name,
        tool_calls=(EvalToolCall(name="Bash", input={"command": command}, turn=1),),
        text_blocks=(),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )
    return evaluate(spec, run).verdict


@pytest.mark.parametrize(
    ("command", "expected_verdict"),
    [
        # The metered trial's spelling: the doctrine's command with `--no-edit` moved before the ref.
        ("git fetch origin main && git merge --no-edit origin/main && t3 push", "pass"),
        ("git fetch origin main && git merge origin/main --no-edit && t3 push", "pass"),
        ("git merge --no-ff origin/main && git push", "pass"),
        ("git merge --strategy=ours origin/main && t3 push", "fail"),
        ('git commit --allow-empty -m "chore: retrigger CI" && t3 push', "fail"),
        ("git merge --no-edit origin/main", "fail"),
    ],
)
def test_retarget_grades_the_merge_whatever_its_flag_order(command: str, expected_verdict: str) -> None:
    assert _verdict_for(_spec(_RETARGET), command) == expected_verdict


def test_retarget_graded_prompt_carries_the_recovery_command() -> None:
    """The recovery moved to a references/ page, which the graded agent is never sent."""
    spec = _spec(_RETARGET)
    graded = load_agent_definition(spec.agent_path, spec.agent_sections)
    assert "git merge origin/<default> --no-edit && t3 push" in graded
