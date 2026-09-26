"""A skip is observable but cannot count as a pass."""

from pathlib import Path

from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, JudgeSpec, Matcher
from teatree.eval.report import JudgeOutcome, evaluate


def test_skipped_scenario_is_not_passed() -> None:
    spec = EvalSpec(
        name="skipped",
        scenario="run a command",
        agent_path="skills/code/SKILL.md",
        prompt="run",
        matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="run_tests"),),
        source_path=Path("spec.yaml"),
    )

    result = evaluate(spec, EvalRun.skipped(spec.name, "credential unavailable"))

    assert result.verdict == "skip"
    assert result.passed is False


def test_skipped_required_judge_is_not_a_pass() -> None:
    spec = EvalSpec(
        name="judge_skipped",
        scenario="run a command",
        agent_path="skills/code/SKILL.md",
        prompt="run",
        matchers=(),
        judge=JudgeSpec(rubric="Check the answer"),
        source_path=Path("spec.yaml"),
    )
    run = EvalRun(
        spec_name=spec.name,
        tool_calls=(),
        text_blocks=("answer",),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )

    result = evaluate(spec, run, judge=lambda _spec, _run: JudgeOutcome(passed=False, skipped=True, rationale=""))

    assert result.passed is False


def test_optional_judge_with_passing_matcher_uses_deterministic_evidence() -> None:
    spec = EvalSpec(
        name="judge_missing",
        scenario="run a command",
        agent_path="skills/code/SKILL.md",
        prompt="run",
        matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="run"),),
        judge=JudgeSpec(rubric="Check the answer"),
        source_path=Path("spec.yaml"),
    )
    run = EvalRun(
        spec_name=spec.name,
        tool_calls=(EvalToolCall(name="Bash", input={"command": "run"}, turn=1),),
        text_blocks=(),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )
    result = evaluate(spec, run)
    assert result.passed is True
