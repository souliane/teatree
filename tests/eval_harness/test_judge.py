"""LLM-judge backend: prompt build, structured verdict, budget, grade dispatch (#1160).

The judge is opt-in per scenario (a ``judge:`` block); matcher-based scenarios
never touch it. It drives ``claude-agent-sdk`` with a ``json_schema`` output
format and reads the ``{verdict, reason}`` off ``ResultMessage.structured_output``
— no regex. When ``claude`` is absent the judge skips, mirroring the runner.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from teatree.eval.judge import (
    JUDGE_DEFAULT_BUDGET_USD,
    ClaudeJudge,
    JudgeBudget,
    JudgeBudgetExceededError,
    build_judge_prompt,
    resolve_judge_budget_usd,
)
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, JudgeSpec, Matcher
from teatree.llm.credentials import AnthropicApiKeyCredential, CredentialError


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


class TestResolveJudgeBudget:
    def test_default_is_the_generous_ceiling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_EVAL_JUDGE_MAX_BUDGET_USD", raising=False)
        assert resolve_judge_budget_usd() == pytest.approx(JUDGE_DEFAULT_BUDGET_USD)

    def test_env_override_raises_the_ceiling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_EVAL_JUDGE_MAX_BUDGET_USD", "2.5")
        assert resolve_judge_budget_usd() == pytest.approx(2.5)

    def test_non_positive_override_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_EVAL_JUDGE_MAX_BUDGET_USD", "0")
        assert resolve_judge_budget_usd() == pytest.approx(JUDGE_DEFAULT_BUDGET_USD)


class TestClaudeJudgeGrade:
    def _grade(self, spec: EvalSpec, messages: list[Any], **kwargs: Any):
        query, captured = _fake_query(messages)
        with (
            patch("teatree.eval.judge.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.eval.judge.query", query),
            # The billed judge call routes through the credential chokepoint; the
            # host has no key, so stub the export (its own coverage lives in
            # TestJudgeAuthenticatesViaTheApiKeyChokepoint).
            patch.object(AnthropicApiKeyCredential, "export", return_value="sk-test"),
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
            patch.object(AnthropicApiKeyCredential, "export", return_value="sk-test"),
        ):
            ClaudeJudge(budget=budget).grade(spec, _run())
            with pytest.raises(JudgeBudgetExceededError):
                ClaudeJudge(budget=budget).grade(spec, _run())

    def test_judge_model_tier_flows_to_options(self) -> None:
        spec = _spec(judge=JudgeSpec(rubric="x", model="sonnet"))
        _verdict, captured = self._grade(spec, [_result(structured_output={"verdict": "PASS", "reason": "ok"})])
        assert captured["options"].model == "sonnet"
        assert captured["options"].max_budget_usd == pytest.approx(JUDGE_DEFAULT_BUDGET_USD)

    def test_judge_budget_cap_fails_one_cell_not_the_whole_suite(self) -> None:
        spec = _spec(judge=JudgeSpec(rubric="x"))

        async def _over_budget(*, prompt: str, options: Any = None, **_: Any) -> AsyncIterator[Any]:
            await asyncio.sleep(0)
            msg = "Claude Code returned an error result: Reached maximum budget ($0.5)"
            raise RuntimeError(msg)
            yield  # pragma: no cover

        with (
            patch("teatree.eval.judge.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.eval.judge.query", _over_budget),
            patch.object(AnthropicApiKeyCredential, "export", return_value="sk-test"),
        ):
            verdict = ClaudeJudge().grade(spec, _run())
        assert verdict.passed is False
        assert verdict.skipped is False
        assert "budget_exceeded" in verdict.rationale

    def test_unknown_judge_error_propagates(self) -> None:
        spec = _spec(judge=JudgeSpec(rubric="x"))

        async def _boom(*, prompt: str, options: Any = None, **_: Any) -> AsyncIterator[Any]:
            await asyncio.sleep(0)
            msg = "kaboom: judge process crashed"
            raise RuntimeError(msg)
            yield  # pragma: no cover

        with (
            patch("teatree.eval.judge.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.eval.judge.query", _boom),
            patch.object(AnthropicApiKeyCredential, "export", return_value="sk-test"),
            pytest.raises(RuntimeError, match="kaboom"),
        ):
            ClaudeJudge().grade(spec, _run())

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
            patch.object(AnthropicApiKeyCredential, "export", return_value="sk-test"),
        ):
            verdict = ClaudeJudge().grade(spec, _run())
        assert verdict.passed is False
        assert verdict.skipped is False
        assert "timed out" in verdict.rationale


class TestJudgeAuthenticatesViaTheApiKeyChokepoint:
    """The metered judge is a billed Claude call, so it routes through the SAME credential chokepoint.

    A judge that actually grades (past the skip guards) makes a metered model
    call, so it must export the metered ``ANTHROPIC_API_KEY`` and fail loud with
    :class:`~teatree.llm.credentials.CredentialError` when no key is resolvable —
    consistent with :func:`teatree.eval.backends.make_runner`. A SKIP path (no
    judge block / skipped run / no ``claude`` binary) grades NO model, so it must
    NOT resolve any credential — a stored-transcript grade with no model call is
    never forced to require a key. Removing the export from ``grade`` turns the
    fail-loud test RED.
    """

    def test_grading_call_exports_the_api_key_before_the_billed_call(self) -> None:
        spec = _spec(judge=JudgeSpec(rubric="x"))
        query, _ = _fake_query([_result(structured_output={"verdict": "PASS", "reason": "ok"})])
        with (
            patch("teatree.eval.judge.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.eval.judge.query", query),
            patch.object(AnthropicApiKeyCredential, "export", return_value="sk-test") as export,
        ):
            ClaudeJudge().grade(spec, _run())
        export.assert_called_once_with()

    def test_grading_call_fails_loud_when_no_api_key_is_resolvable(self) -> None:
        # claude present + a real run to grade, but no env key and an empty pass
        # store → the billed judge call must fail loud, never authenticate as
        # nothing. The export is reached only after the skip guards.
        spec = _spec(judge=JudgeSpec(rubric="x"))
        query, _ = _fake_query([_result(structured_output={"verdict": "PASS", "reason": "ok"})])
        with (
            patch("teatree.eval.judge.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.eval.judge.query", query),
            patch.dict("os.environ", {}, clear=False),
            patch("teatree.llm.credentials.read_pass", return_value=""),
        ):
            os.environ.pop(AnthropicApiKeyCredential.spec.env_var, None)
            with pytest.raises(CredentialError):
                ClaudeJudge().grade(spec, _run())

    def test_no_judge_block_never_resolves_a_credential(self) -> None:
        with patch.object(AnthropicApiKeyCredential, "export") as export:
            verdict = ClaudeJudge().grade(_spec(), _run())
        assert verdict.skipped is True
        export.assert_not_called()

    def test_skipped_run_never_resolves_a_credential(self) -> None:
        spec = _spec(judge=JudgeSpec(rubric="x"))
        with patch.object(AnthropicApiKeyCredential, "export") as export:
            verdict = ClaudeJudge().grade(spec, _run(terminal_reason="skipped: no transcript"))
        assert verdict.skipped is True
        export.assert_not_called()

    def test_missing_claude_binary_never_resolves_a_credential(self) -> None:
        # A transcript-grade-only / keyless contributor with no claude binary
        # grades no model, so it must not be forced to require a key.
        spec = _spec(judge=JudgeSpec(rubric="x"))
        with (
            patch("teatree.eval.judge.shutil.which", return_value=None),
            patch.object(AnthropicApiKeyCredential, "export") as export,
        ):
            verdict = ClaudeJudge().grade(spec, _run())
        assert verdict.skipped is True
        export.assert_not_called()
