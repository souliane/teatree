"""A suite cap leaves incomplete coverage but never hides a measured failure."""

import dataclasses
import json
from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.eval.multi_trial import run_pass_at_k_lane
from teatree.eval.backends import ANTHROPIC_API_BACKEND, TRANSCRIPT_BACKEND
from teatree.eval.cost_observation import ConservativeSuiteBudget
from teatree.eval.judge import ModelSeamJudge
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, JudgeSpec, Matcher


def _spec(name: str) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=name,
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="done"),),
        source_path=Path("spec.yaml"),
    )


def _run(spec: EvalSpec, *, failed: bool) -> EvalRun:
    return EvalRun(
        spec_name=spec.name,
        tool_calls=() if failed else (EvalToolCall(name="Bash", input={"command": "done"}, turn=1),),
        text_blocks=("done",),
        terminal_reason="end_turn",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
        cost_usd=0.01,
    )


class _CappedRunner:
    def __init__(self, *, failed: bool) -> None:
        self.failed = failed
        self.budget_exhausted = False

    def run(self, spec: EvalSpec) -> EvalRun:
        if spec.name == "later":
            self.budget_exhausted = True
            return EvalRun.skipped(spec.name, "coverage incomplete: suite budget exhausted")
        return _run(spec, failed=self.failed)


@pytest.mark.parametrize(("failed", "expected"), [(False, 75), (True, 1)])
def test_weekly_cap_preserves_red_and_marks_incomplete(
    monkeypatch: pytest.MonkeyPatch, *, failed: bool, expected: int
) -> None:
    runner = _CappedRunner(failed=failed)
    monkeypatch.setattr("teatree.cli.eval.single_trial.make_runner", lambda *args, **kwargs: runner)
    monkeypatch.setattr("teatree.cli.eval.app.discover_specs", lambda: [_spec("earlier"), _spec("later")])
    monkeypatch.setattr("teatree.cli.eval.app.ensure_django", lambda: None)
    result = CliRunner().invoke(app, ["eval", "run", "--backend", TRANSCRIPT_BACKEND, "--no-persist"])
    assert result.exit_code == expected, result.output


def test_nightly_failure_wins_over_exhausted_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _CappedRunner(failed=True)
    monkeypatch.setattr("teatree.cli.eval.multi_trial.make_runner", lambda *args, **kwargs: runner)
    with pytest.raises(SystemExit) as exc:
        run_pass_at_k_lane(
            [_spec("earlier"), _spec("later")],
            backend=ANTHROPIC_API_BACKEND,
            max_turns=None,
            trials=1,
            require="any",
            output_format="text",
        )
    assert exc.value.code == 1


def test_judge_only_cap_is_incomplete_through_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = EvalSpec(
        name="judge_cap",
        scenario="judge an answer",
        agent_path="skills/code/SKILL.md",
        prompt="answer",
        matchers=(),
        judge=JudgeSpec(rubric="clear answer", model="claude-haiku-4-5"),
        source_path=Path("spec.yaml"),
    )
    monkeypatch.setattr("teatree.cli.eval.app.discover_specs", lambda: [spec])
    monkeypatch.setattr("teatree.cli.eval.app.ensure_django", lambda: None)
    monkeypatch.setattr(
        "teatree.cli.eval.single_trial.make_runner", lambda *args, **kwargs: _CappedRunner(failed=False)
    )
    monkeypatch.setattr("teatree.eval.judge.suite_budget_from_env", lambda: ConservativeSuiteBudget(0.000001))
    monkeypatch.setattr(
        "teatree.cli.eval.run_modes.ModelSeamJudge",
        lambda *, budget: ModelSeamJudge(budget=budget, model=TestModel()),
    )
    result = CliRunner().invoke(
        app,
        ["eval", "run", "--backend", ANTHROPIC_API_BACKEND, "--judge", "--local", "--no-persist", "--format", "json"],
    )
    assert result.exit_code == 75, result.output
    payload = json.loads(result.stdout)
    assert payload["scenarios"][0]["verdict"] == "incomplete"
    assert payload["summary"]["failed"] == 0
    assert payload["summary"]["incomplete"] == 1
    failed_spec = dataclasses.replace(spec, matchers=_spec("matcher").matchers)
    monkeypatch.setattr("teatree.cli.eval.app.discover_specs", lambda: [failed_spec])
    monkeypatch.setattr("teatree.cli.eval.single_trial.make_runner", lambda *args, **kwargs: _CappedRunner(failed=True))
    failed = CliRunner().invoke(
        app,
        ["eval", "run", "--backend", ANTHROPIC_API_BACKEND, "--judge", "--local", "--no-persist", "--format", "json"],
    )
    assert failed.exit_code == 1, failed.output
    assert json.loads(failed.stdout)["scenarios"][0]["verdict"] == "fail"
