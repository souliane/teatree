"""LLM-judge backend: prompt build, structured verdict, budget, grade dispatch (#1160).

The judge is opt-in per scenario (a ``judge:`` block); matcher-based scenarios
never touch it. It drives ``claude-agent-sdk`` with a ``json_schema`` output
format and reads the ``{verdict, reason}`` off ``ResultMessage.structured_output``
— no regex. When ``claude`` is absent the judge skips, mirroring the runner.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from teatree.eval.judge import ClaudeJudge, JudgeBudget, JudgeBudgetExceededError, build_judge_prompt
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


def _result(*, structured_output: Any = None, subtype: str = "success") -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=5,
        duration_api_ms=4,
        is_error=False,
        num_turns=1,
        session_id="s1",
        total_cost_usd=0.001,
        result="(judge reply)",
        structured_output=structured_output,
    )


def _fake_query(messages: list[Any]):
    captured: dict[str, Any] = {}

    async def _query(*, prompt: str, options: Any = None, **_: Any) -> AsyncIterator[Any]:
        await asyncio.sleep(0)
        captured["prompt"] = prompt
        captured["options"] = options
        for message in messages:
            yield message

    return _query, captured


class TestBuildJudgePrompt:
    def test_includes_rubric_and_text_but_not_full_tool_inputs(self) -> None:
        spec = _spec(judge=JudgeSpec(rubric="The explanation names the migration."))
        prompt = build_judge_prompt(spec, _run())
        assert "The explanation names the migration." in prompt
        assert "Here is the explanation." in prompt
        assert "Bash(command, timeout)" in prompt
        assert "zzqqxx" not in prompt

    def test_requires_a_judge_block(self) -> None:
        with pytest.raises(ValueError, match="judge block"):
            build_judge_prompt(_spec(), _run())


class TestJudgeBudget:
    def test_consume_until_exhausted(self) -> None:
        budget = JudgeBudget(max_calls=2)
        budget.consume()
        budget.consume()
        with pytest.raises(JudgeBudgetExceededError):
            budget.consume()


class TestClaudeJudgeGrade:
    def _grade(self, spec: EvalSpec, messages: list[Any], **kwargs: Any):
        query, captured = _fake_query(messages)
        with (
            patch("teatree.eval.judge.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.eval.judge.query", query),
        ):
            verdict = ClaudeJudge(**kwargs).grade(spec, _run())
        return verdict, captured

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

    def test_structured_pass_verdict_passes(self) -> None:
        spec = _spec(judge=JudgeSpec(rubric="x"))
        verdict, _ = self._grade(spec, [_result(structured_output={"verdict": "PASS", "reason": "matches diff"})])
        assert verdict.passed is True
        assert verdict.skipped is False
        assert "matches diff" in verdict.rationale

    def test_structured_fail_verdict_fails(self) -> None:
        spec = _spec(judge=JudgeSpec(rubric="x"))
        verdict, _ = self._grade(spec, [_result(structured_output={"verdict": "FAIL", "reason": "omitted migration"})])
        assert verdict.passed is False
        assert verdict.skipped is False
        assert "omitted migration" in verdict.rationale

    def test_missing_structured_output_fails(self) -> None:
        # A judge that cannot commit to a structured verdict must not pass.
        spec = _spec(judge=JudgeSpec(rubric="x"))
        verdict, _ = self._grade(spec, [_result(structured_output=None)])
        assert verdict.passed is False
        assert verdict.skipped is False

    def test_no_result_message_fails(self) -> None:
        spec = _spec(judge=JudgeSpec(rubric="x"))
        verdict, _ = self._grade(spec, [])
        assert verdict.passed is False
        assert verdict.skipped is False

    def test_leading_non_result_messages_are_skipped(self) -> None:
        # The judge ignores assistant chatter and reads the verdict off the result.
        spec = _spec(judge=JudgeSpec(rubric="x"))
        messages = [
            AssistantMessage(content=[TextBlock(text="thinking out loud")], model="sonnet"),
            _result(structured_output={"verdict": "PASS", "reason": "ok"}),
        ]
        verdict, _ = self._grade(spec, messages)
        assert verdict.passed is True

    def test_unexpected_verdict_value_fails(self) -> None:
        spec = _spec(judge=JudgeSpec(rubric="x"))
        verdict, _ = self._grade(spec, [_result(structured_output={"verdict": "MAYBE", "reason": "unsure"})])
        assert verdict.passed is False
        assert verdict.skipped is False
        assert "PASS/FAIL" in verdict.rationale

    def test_budget_is_consumed_per_call(self) -> None:
        spec = _spec(judge=JudgeSpec(rubric="x"))
        budget = JudgeBudget(max_calls=1)
        query, _ = _fake_query([_result(structured_output={"verdict": "PASS", "reason": "ok"})])
        with (
            patch("teatree.eval.judge.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.eval.judge.query", query),
        ):
            ClaudeJudge(budget=budget).grade(spec, _run())
            with pytest.raises(JudgeBudgetExceededError):
                ClaudeJudge(budget=budget).grade(spec, _run())

    def test_judge_model_tier_flows_to_options(self) -> None:
        spec = _spec(judge=JudgeSpec(rubric="x", model="sonnet"))
        _verdict, captured = self._grade(spec, [_result(structured_output={"verdict": "PASS", "reason": "ok"})])
        assert captured["options"].model == "sonnet"
        assert captured["options"].max_budget_usd is not None

    def test_json_schema_output_format_requested(self) -> None:
        spec = _spec(judge=JudgeSpec(rubric="x"))
        _verdict, captured = self._grade(spec, [_result(structured_output={"verdict": "PASS", "reason": "ok"})])
        output_format = captured["options"].output_format
        assert output_format is not None
        assert output_format["type"] == "json_schema"
        assert "verdict" in output_format["schema"]["properties"]
        assert "reason" in output_format["schema"]["properties"]

    def test_timeout_fails(self) -> None:
        spec = _spec(judge=JudgeSpec(rubric="x"))

        async def _slow(*, prompt: str, options: Any = None, **_: Any) -> AsyncIterator[Any]:
            await asyncio.sleep(0)
            raise TimeoutError
            yield  # pragma: no cover

        with (
            patch("teatree.eval.judge.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.eval.judge.query", _slow),
            patch("teatree.eval.judge.WATCHDOG_SECONDS", 0.01),
        ):
            verdict = ClaudeJudge().grade(spec, _run())
        assert verdict.passed is False
        assert verdict.skipped is False
        assert "timed out" in verdict.rationale
