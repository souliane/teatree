"""LLM-judge backend: prompt build, verdict parse, budget, grade dispatch (#1160).

The judge is opt-in per scenario (a ``judge:`` block); matcher-based scenarios
never touch it. When ``claude`` is absent the judge skips, mirroring the runner.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.eval.judge import (
    ClaudeJudge,
    JudgeBudget,
    JudgeBudgetExceededError,
    build_judge_prompt,
    parse_judge_verdict,
)
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, JudgeSpec, Matcher


def _spec(*, judge: JudgeSpec | None = None) -> EvalSpec:
    return EvalSpec(
        name="explains_faithfully",
        scenario="agent explains the change faithfully",
        agent_path="skills/code/SKILL.md",
        prompt="explain",
        matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),),
        source_path=Path("/tmp/spec.yaml"),
        judge=judge,
    )


def _run(*, terminal_reason: str = "success") -> EvalRun:
    return EvalRun(
        spec_name="explains_faithfully",
        tool_calls=(EvalToolCall(name="Bash", input={"command": "zzqqxx", "timeout": 5}, turn=1),),
        text_blocks=("Here is the explanation.",),
        terminal_reason=terminal_reason,
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )


class _FakeResult:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


class TestParseJudgeVerdict:
    def test_pass(self) -> None:
        passed, rationale = parse_judge_verdict("PASS — the explanation matches the diff")
        assert passed is True
        assert "explanation" in rationale

    def test_fail(self) -> None:
        passed, _ = parse_judge_verdict("FAIL: it omitted the migration")
        assert passed is False

    def test_no_verdict_defaults_to_fail(self) -> None:
        passed, rationale = parse_judge_verdict("I am not sure about this one")
        assert passed is False
        assert "no PASS/FAIL" in rationale

    def test_case_insensitive(self) -> None:
        passed, _ = parse_judge_verdict("pass, looks good")
        assert passed is True


class TestBuildJudgePrompt:
    def test_includes_rubric_and_text_but_not_full_tool_inputs(self) -> None:
        spec = _spec(judge=JudgeSpec(rubric="The explanation names the migration."))
        prompt = build_judge_prompt(spec, _run())
        assert "The explanation names the migration." in prompt
        assert "Here is the explanation." in prompt
        assert "Bash(command, timeout)" in prompt
        assert "zzqqxx" not in prompt


class TestJudgeBudget:
    def test_consume_until_exhausted(self) -> None:
        budget = JudgeBudget(max_calls=2)
        budget.consume()
        budget.consume()
        with pytest.raises(JudgeBudgetExceededError):
            budget.consume()


class TestClaudeJudgeGrade:
    def test_no_judge_block_is_skipped(self) -> None:
        verdict = ClaudeJudge().grade(_spec(), _run())
        assert verdict.skipped is True
        assert verdict.passed is True

    def test_skipped_run_is_skipped(self) -> None:
        spec = _spec(judge=JudgeSpec(rubric="x"))
        verdict = ClaudeJudge().grade(spec, _run(terminal_reason="skipped: claude binary not on PATH"))
        assert verdict.skipped is True

    def test_missing_claude_binary_is_skipped(self) -> None:
        spec = _spec(judge=JudgeSpec(rubric="x"))
        with patch("teatree.eval.judge.shutil.which", return_value=None):
            verdict = ClaudeJudge().grade(spec, _run())
        assert verdict.skipped is True

    def test_grade_passes_when_judge_says_pass(self) -> None:
        spec = _spec(judge=JudgeSpec(rubric="x"))
        with (
            patch("teatree.eval.judge.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.eval.judge.run_allowed_to_fail", return_value=_FakeResult("PASS good")),
        ):
            verdict = ClaudeJudge().grade(spec, _run())
        assert verdict.passed is True
        assert verdict.skipped is False

    def test_grade_fails_when_judge_says_fail(self) -> None:
        spec = _spec(judge=JudgeSpec(rubric="x"))
        with (
            patch("teatree.eval.judge.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.eval.judge.run_allowed_to_fail", return_value=_FakeResult("FAIL nope")),
        ):
            verdict = ClaudeJudge().grade(spec, _run())
        assert verdict.passed is False

    def test_budget_is_consumed_per_call(self) -> None:
        spec = _spec(judge=JudgeSpec(rubric="x"))
        budget = JudgeBudget(max_calls=1)
        with (
            patch("teatree.eval.judge.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.eval.judge.run_allowed_to_fail", return_value=_FakeResult("PASS")),
        ):
            ClaudeJudge(budget=budget).grade(spec, _run())
            with pytest.raises(JudgeBudgetExceededError):
                ClaudeJudge(budget=budget).grade(spec, _run())

    def test_judge_model_tier_is_in_command(self) -> None:
        spec = _spec(judge=JudgeSpec(rubric="x", model="sonnet"))
        captured: dict[str, list[str]] = {}

        def _fake_run(command, **_):
            captured["cmd"] = command
            return _FakeResult("PASS")

        with (
            patch("teatree.eval.judge.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.eval.judge.run_allowed_to_fail", _fake_run),
        ):
            ClaudeJudge().grade(spec, _run())
        assert "sonnet" in captured["cmd"]
        assert "--max-budget-usd" in captured["cmd"]
