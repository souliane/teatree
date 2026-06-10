"""In-process ``claude-agent-sdk`` eval runner.

The runner drives ``claude_agent_sdk.query`` per scenario, collects the typed
messages, and produces an :class:`~teatree.eval.models.EvalRun` byte-identical in
shape to the deleted ``claude -p`` runner. Grading (report.py) is unchanged, so
the swap is invisible to the grader. The SDK is mocked here — no metered calls.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from teatree.eval.models import EvalSpec, Matcher
from teatree.eval.sdk_runner import (
    MAX_BUDGET_USD,
    BudgetExceededError,
    ClaudeCliMissingError,
    CleanRoomConfig,
    SdkInProcessRunner,
    build_sdk_options,
    classify_terminal_error,
)


def _spec(tmp_path: Path, *, max_turns: int = 3, model: str = "haiku", tools: tuple[str, ...] = ("Bash",)) -> EvalSpec:
    agent = tmp_path / "agent.md"
    agent.write_text("# fake skill\n\nbody\n", encoding="utf-8")
    return EvalSpec(
        name="worktree_first",
        scenario="agent must create a worktree first",
        agent_path=str(agent),
        prompt="Fix README.md typo.",
        matchers=(
            Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="git worktree add"),
        ),
        source_path=tmp_path / "spec.yaml",
        model=model,
        max_turns=max_turns,
        tools=tools,
    )


def _fake_query(messages: list[Any]):
    """Build a stand-in for ``claude_agent_sdk.query`` yielding *messages*.

    The real ``query`` is an async generator keyword-only on ``prompt``; the
    runner consumes it via ``asyncio.run``. The stub records the options it was
    called with so isolation/clean-room assertions can inspect them.
    """
    captured: dict[str, Any] = {}

    async def _query(*, prompt: str, options: Any = None, **_: Any) -> AsyncIterator[Any]:
        await asyncio.sleep(0)
        captured["prompt"] = prompt
        captured["options"] = options
        for message in messages:
            yield message

    return _query, captured


def _result(
    *, subtype: str = "success", is_error: bool = False, total_cost_usd: float | None = 0.0123, num_turns: int = 2
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=10,
        duration_api_ms=8,
        is_error=is_error,
        num_turns=num_turns,
        session_id="s1",
        total_cost_usd=total_cost_usd,
        result="ok",
    )


class TestSdkInProcessRunnerSkip:
    def test_returns_skip_run_when_claude_missing(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        with patch("teatree.eval.sdk_runner.shutil.which", return_value=None):
            result = SdkInProcessRunner().run(spec)
        assert result.terminal_reason.startswith("skipped:")
        assert result.is_error is False
        assert result.tool_calls == ()

    def test_require_executed_hard_errors_when_claude_missing(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value=None),
            pytest.raises(ClaudeCliMissingError),
        ):
            SdkInProcessRunner(require_executed=True).run(spec)


class TestSdkInProcessRunnerCapture:
    def _run(self, spec: EvalSpec, messages: list[Any], **kwargs: Any):
        query, captured = _fake_query(messages)
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            run = SdkInProcessRunner(**kwargs).run(spec)
        return run, captured

    def test_captures_tool_calls_text_terminal_cost(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        messages = [
            AssistantMessage(
                content=[
                    TextBlock(text="Creating a worktree first."),
                    ToolUseBlock(id="t1", name="Bash", input={"command": "git worktree add ../wt HEAD"}),
                ],
                model="haiku",
            ),
            AssistantMessage(
                content=[ToolUseBlock(id="t2", name="Bash", input={"command": "echo done"})],
                model="haiku",
            ),
            _result(total_cost_usd=0.0456),
        ]
        run, _ = self._run(spec, messages)
        assert run.terminal_reason == "success"
        assert run.is_error is False
        assert len(run.tool_calls) == 2
        assert run.tool_calls[0].name == "Bash"
        assert run.tool_calls[0].input["command"].startswith("git worktree add")
        assert run.tool_calls[0].turn == 1
        assert run.tool_calls[1].turn == 2
        assert run.text_blocks == ("Creating a worktree first.",)
        assert run.cost_usd == pytest.approx(0.0456)

    def test_error_result_marks_is_error(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        messages = [_result(subtype="error_max_turns", is_error=True, total_cost_usd=0.01)]
        run, _ = self._run(spec, messages)
        assert run.terminal_reason == "error_max_turns"
        assert run.is_error is True

    def test_no_result_message_is_aborted(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        messages = [AssistantMessage(content=[TextBlock(text="hi")], model="haiku")]
        run, _ = self._run(spec, messages)
        assert run.terminal_reason == "aborted"
        assert run.is_error is True

    def test_zero_cost_metered_run_records_zero(self, tmp_path: Path) -> None:
        # The unmetered-sdk guard relies on cost_usd==0.0 surfacing here.
        spec = _spec(tmp_path)
        messages = [_result(total_cost_usd=None)]
        run, _ = self._run(spec, messages)
        assert run.cost_usd == pytest.approx(0.0)

    def test_max_turns_override_takes_precedence(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, max_turns=3)
        _run, captured = self._run(spec, [_result()], max_turns_override=9)
        assert captured["options"].max_turns == 9

    def test_spec_max_turns_used_without_override(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, max_turns=3)
        _run, captured = self._run(spec, [_result()])
        assert captured["options"].max_turns == 3

    def test_prompt_and_model_and_tools_flow_to_options(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, model="haiku", tools=("Bash", "Read"))
        _run, captured = self._run(spec, [_result()])
        assert captured["prompt"] == "Fix README.md typo."
        assert captured["options"].model == "haiku"
        assert list(captured["options"].allowed_tools) == ["Bash", "Read"]

    def test_budget_breaker_is_set(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        _run, captured = self._run(spec, [_result()])
        assert captured["options"].max_budget_usd == pytest.approx(float(MAX_BUDGET_USD))

    def test_max_budget_usd_override_flows_to_options(self, tmp_path: Path) -> None:
        # A non-default per-run budget (the benchmark's generous cap) threads from
        # the runner through CleanRoomConfig into ClaudeAgentOptions.max_budget_usd.
        spec = _spec(tmp_path)
        _run, captured = self._run(spec, [_result()], max_budget_usd=2.0)
        assert captured["options"].max_budget_usd == pytest.approx(2.0)

    def test_model_at_effort_tag_splits_into_model_and_effort_options(self, tmp_path: Path) -> None:
        # ClaudeAgentOptions.effort is the SDK's first-class reasoning-effort
        # field (the transport renders it as the CLI's `--effort <level>` flag).
        spec = _spec(tmp_path, model="claude-opus-4-8@xhigh")
        _run, captured = self._run(spec, [_result()])
        assert captured["options"].model == "claude-opus-4-8"
        assert captured["options"].effort == "xhigh"

    def test_plain_model_leaves_effort_unset(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, model="claude-fable-5")
        _run, captured = self._run(spec, [_result()])
        assert captured["options"].model == "claude-fable-5"
        assert captured["options"].effort is None

    def test_timeout_yields_timeout_run(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)

        async def _slow_query(*, prompt: str, options: Any = None, **_: Any) -> AsyncIterator[Any]:
            await asyncio.sleep(0)
            raise TimeoutError
            yield  # pragma: no cover

        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", _slow_query),
            patch("teatree.eval.sdk_runner.WATCHDOG_SECONDS", 0.01),
        ):
            run = SdkInProcessRunner(workspace=tmp_path).run(spec)
        assert run.terminal_reason == "timeout"
        assert run.is_error is True


class TestSdkInProcessRunnerAgentDefinition:
    def _run(self, spec: EvalSpec, **kwargs: Any):
        query, captured = _fake_query([_result()])
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            SdkInProcessRunner(**kwargs).run(spec)
        return captured

    def test_raises_when_agent_definition_missing(self, tmp_path: Path) -> None:
        spec = EvalSpec(
            name="bad",
            scenario="bad",
            agent_path=str(tmp_path / "does-not-exist.md"),
            prompt="x",
            matchers=(),
            source_path=tmp_path / "spec.yaml",
        )
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            pytest.raises(FileNotFoundError),
        ):
            SdkInProcessRunner(workspace=tmp_path).run(spec)

    def test_raises_when_agent_definition_empty(self, tmp_path: Path) -> None:
        agent = tmp_path / "empty.md"
        agent.write_text("", encoding="utf-8")
        spec = EvalSpec(
            name="empty",
            scenario="empty",
            agent_path=str(agent),
            prompt="x",
            matchers=(),
            source_path=tmp_path / "spec.yaml",
        )
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            pytest.raises(ValueError, match="empty"),
        ):
            SdkInProcessRunner(workspace=tmp_path).run(spec)

    def test_agent_sections_send_only_named_section_as_system_prompt(self, tmp_path: Path) -> None:
        agent = tmp_path / "rules.md"
        agent.write_text(
            "# Agent Rules\n\nframing\n\n"
            "## Background Long Operations\n\nBackground >15s work.\n\n"
            "## Unrelated Other Rule\n\nfifty other rules here.\n",
            encoding="utf-8",
        )
        spec = EvalSpec(
            name="bg",
            scenario="background long ops",
            agent_path=str(agent),
            prompt="x",
            matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),),
            source_path=tmp_path / "spec.yaml",
            agent_sections=("Background Long Operations",),
        )
        captured = self._run(spec, workspace=tmp_path)
        system_prompt = captured["options"].system_prompt
        assert "Background >15s work." in system_prompt
        assert "fifty other rules here." not in system_prompt

    def test_no_agent_sections_sends_whole_file(self, tmp_path: Path) -> None:
        agent = tmp_path / "rules.md"
        full = "# Agent Rules\n\n## A\n\naaa\n\n## B\n\nbbb\n"
        agent.write_text(full, encoding="utf-8")
        spec = EvalSpec(
            name="full",
            scenario="full file",
            agent_path=str(agent),
            prompt="x",
            matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),),
            source_path=tmp_path / "spec.yaml",
        )
        captured = self._run(spec, workspace=tmp_path)
        assert captured["options"].system_prompt == full


class TestSdkInProcessRunnerMessageMapping:
    def test_unknown_block_and_non_content_message_are_handled(self, tmp_path: Path) -> None:
        from claude_agent_sdk import SystemMessage, ThinkingBlock  # noqa: PLC0415

        spec = _spec(tmp_path)
        messages = [
            SystemMessage(subtype="init", data={}),
            AssistantMessage(
                content=[
                    ThinkingBlock(thinking="hmm", signature="sig"),
                    ToolUseBlock(id="t1", name="Bash", input={"command": "ls"}),
                ],
                model="haiku",
            ),
            _result(),
        ]
        query, _ = _fake_query(messages)
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            run = SdkInProcessRunner(workspace=tmp_path).run(spec)
        # The thinking block is dropped, the tool_use is captured, the system message ignored.
        assert [c.name for c in run.tool_calls] == ["Bash"]
        assert run.text_blocks == ()

    def test_relative_agent_path_resolves_against_teatree_root(self, tmp_path: Path, monkeypatch) -> None:
        # cwd is a temp dir (first candidate misses), so resolution falls through
        # to the teatree-root candidate (exercises the continue + found branch).
        from teatree.eval.sdk_runner import _teatree_root  # noqa: PLC0415

        monkeypatch.chdir(tmp_path)
        rel = "skills/code/SKILL.md"
        assert (_teatree_root() / rel).is_file(), "test assumes the SKILL.md exists at the teatree root"
        spec = EvalSpec(
            name="rel",
            scenario="x",
            agent_path=rel,
            prompt="x",
            matchers=(),
            source_path=tmp_path / "spec.yaml",
        )
        query, captured = _fake_query([_result()])
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            SdkInProcessRunner(workspace=tmp_path).run(spec)
        assert captured["options"].system_prompt.strip()

    def test_relative_agent_path_not_found_raises(self, tmp_path: Path, monkeypatch) -> None:
        # No candidate resolves (cwd and teatree-root both miss) -> the loop
        # exhausts and the missing-file raise fires.
        monkeypatch.chdir(tmp_path)
        spec = EvalSpec(
            name="missing_rel",
            scenario="x",
            agent_path="no/such/relative/SKILL.md",
            prompt="x",
            matchers=(),
            source_path=tmp_path / "spec.yaml",
        )
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            pytest.raises(FileNotFoundError),
        ):
            SdkInProcessRunner(workspace=tmp_path).run(spec)


def _config(tmp_path: Path, *, max_budget_usd: float) -> CleanRoomConfig:
    return CleanRoomConfig(
        system_prompt="sp",
        workspace=tmp_path,
        cwd=str(tmp_path),
        env={},
        allowed_tools=("Bash",),
        model="haiku",
        max_turns=3,
        max_budget_usd=max_budget_usd,
    )


class TestBuildSdkOptionsBudget:
    def test_default_budget_is_the_cheap_lane_constant(self, tmp_path: Path) -> None:
        config = _config(tmp_path, max_budget_usd=float(MAX_BUDGET_USD))
        assert build_sdk_options(config).max_budget_usd == pytest.approx(float(MAX_BUDGET_USD))

    def test_non_default_budget_carries_into_options(self, tmp_path: Path) -> None:
        # build_sdk_options must read the budget from the config, NOT the constant —
        # this is the seam the benchmark's generous cap threads through.
        config = _config(tmp_path, max_budget_usd=2.5)
        assert build_sdk_options(config).max_budget_usd == pytest.approx(2.5)


def _budget_raising_query(message: str):
    """A fake ``query`` that raises a bare Exception(message) like the SDK's budget path."""

    async def _query(*, prompt: str, options: Any = None, **_: Any) -> AsyncIterator[Any]:
        await asyncio.sleep(0)
        raise Exception(message)  # noqa: TRY002 — the SDK raises a BARE Exception for the budget breaker; this fake must reproduce that exact class so the runner's message-based catch is exercised.
        yield  # pragma: no cover

    return _query


class TestSdkInProcessRunnerBudgetExceeded:
    def _run_with_raising_query(self, spec: EvalSpec, query, **kwargs: Any):
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            return SdkInProcessRunner(workspace=spec.source_path.parent, **kwargs).run(spec)

    def test_budget_exceeded_is_a_recorded_run_not_a_crash(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        query = _budget_raising_query("Claude Code returned an error result: Reached maximum budget ($0.1)")
        run = self._run_with_raising_query(spec, query)
        assert run.is_error is True
        assert run.terminal_reason == "budget_exceeded"
        assert run.tool_calls == ()

    def test_budget_exceeded_recovers_the_partial_cost_from_the_message(self, tmp_path: Path) -> None:
        # The cap floor is recoverable from the "($0.1)" the SDK message carries,
        # so an over-budget cell renders a real cost, not a blank.
        spec = _spec(tmp_path)
        query = _budget_raising_query("Claude Code returned an error result: Reached maximum budget ($0.1)")
        run = self._run_with_raising_query(spec, query)
        assert run.cost_usd == pytest.approx(0.1)

    def test_budget_exceeded_falls_back_to_cap_when_message_carries_no_amount(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        query = _budget_raising_query("Claude Code returned an error result: Reached maximum budget")
        run = self._run_with_raising_query(spec, query, max_budget_usd=2.0)
        assert run.cost_usd == pytest.approx(2.0)

    def test_typed_budget_exceeded_error_is_caught(self, tmp_path: Path) -> None:
        # A directly-raised BudgetExceededError (the typed alias) is also a run.
        spec = _spec(tmp_path)

        async def _query(*, prompt: str, options: Any = None, **_: Any) -> AsyncIterator[Any]:
            await asyncio.sleep(0)
            message = "Reached maximum budget ($0.1)"
            raise BudgetExceededError(message)
            yield  # pragma: no cover

        run = self._run_with_raising_query(spec, _query)
        assert run.terminal_reason == "budget_exceeded"

    def test_non_budget_exception_still_propagates(self, tmp_path: Path) -> None:
        # Anti-vacuity: the catch is defensive, NOT a blanket swallow. An unrelated
        # error must surface (a swallowed one would hide a real crash).
        spec = _spec(tmp_path)
        query = _budget_raising_query("Claude Code returned an error result: error_during_execution")
        with pytest.raises(Exception, match="error_during_execution"):
            self._run_with_raising_query(spec, query)

    def test_budget_exceeded_run_grades_to_a_fail_with_visible_cost(self, tmp_path: Path) -> None:
        # An over-budget cell is a real measurement: it grades to FAIL (not skip),
        # carrying the cap cost so the benchmark renders it legibly, not blank.
        from teatree.eval.report import evaluate  # noqa: PLC0415

        spec = _spec(tmp_path)
        query = _budget_raising_query("Claude Code returned an error result: Reached maximum budget ($0.1)")
        run = self._run_with_raising_query(spec, query)
        result = evaluate(spec, run)
        assert result.skipped is False
        assert result.passed is False
        assert result.verdict == "fail"
        assert result.run.cost_usd == pytest.approx(0.1)


def _yield_then_raise_query(messages: list[Any], message: str):
    """A fake ``query`` that yields *messages*, THEN raises a bare ``Exception``.

    Models the SDK's real terminal-result shape (``query.py`` ``receive_messages``
    L852): every message gathered before the cap reaches the consumer's
    ``async for`` loop, then the trailing error sentinel raises a bare
    ``Exception``. The runner must keep the partial trajectory, not discard it.
    """

    async def _query(*, prompt: str, options: Any = None, **_: Any) -> AsyncIterator[Any]:
        await asyncio.sleep(0)
        for item in messages:
            yield item
        raise Exception(message)  # noqa: TRY002 — the SDK raises a BARE Exception mid-stream for any error-result subtype (budget, max-turns); this fake must reproduce that exact class so the runner's message-based classifier is exercised.

    return _query


class TestClassifyTerminalError:
    def test_classifies_the_sdk_max_budget_message(self) -> None:
        msg = "Claude Code returned an error result: Reached maximum budget ($0.1)"
        assert classify_terminal_error(msg) == "budget_exceeded"

    def test_classifies_the_sdk_max_turns_message(self) -> None:
        # The metered second-crash string: error_max_turns surfaces as
        # "Reached maximum number of turns (3)".
        msg = "Claude Code returned an error result: Reached maximum number of turns (3)"
        assert classify_terminal_error(msg) == "max_turns"

    def test_returns_none_for_a_genuine_error(self) -> None:
        assert classify_terminal_error("Claude Code returned an error result: error_during_execution") is None
        assert classify_terminal_error("some other RuntimeError about a socket") is None


class TestSdkInProcessRunnerMaxTurnsCapturesTrajectory:
    def _run_with_query(self, spec: EvalSpec, query, **kwargs: Any):
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            return SdkInProcessRunner(workspace=spec.source_path.parent, **kwargs).run(spec)

    def test_max_turns_cap_keeps_the_tool_call_emitted_before_the_cap(self, tmp_path: Path) -> None:
        # The agent DID the expected tool call before turn 3, then hit the cap.
        # The partial trajectory must survive the bare Exception — not be discarded.
        spec = _spec(tmp_path)
        messages = [
            AssistantMessage(
                content=[
                    TextBlock(text="Creating a worktree first."),
                    ToolUseBlock(id="t1", name="Bash", input={"command": "git worktree add ../wt HEAD"}),
                ],
                model="haiku",
            ),
        ]
        query = _yield_then_raise_query(
            messages, "Claude Code returned an error result: Reached maximum number of turns (3)"
        )
        run = self._run_with_query(spec, query)
        assert run.terminal_reason == "max_turns"
        assert len(run.tool_calls) == 1
        assert run.tool_calls[0].name == "Bash"
        assert run.tool_calls[0].input["command"].startswith("git worktree add")
        assert run.text_blocks == ("Creating a worktree first.",)

    def test_max_turns_cap_with_satisfying_trajectory_grades_to_pass(self, tmp_path: Path) -> None:
        # is_error decision: a capped run that captured a trajectory lets the
        # matchers grade it. The agent satisfied the positive matcher before the
        # cap, so the scenario PASSES — the cap is surfaced via terminal_reason,
        # not forced into a FAIL.
        from teatree.eval.report import evaluate  # noqa: PLC0415

        spec = _spec(tmp_path)
        messages = [
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Bash", input={"command": "git worktree add ../wt HEAD"})],
                model="haiku",
            ),
        ]
        query = _yield_then_raise_query(
            messages, "Claude Code returned an error result: Reached maximum number of turns (3)"
        )
        run = self._run_with_query(spec, query)
        assert run.is_error is False
        result = evaluate(spec, run)
        assert result.skipped is False
        assert result.passed is True
        assert result.verdict == "pass"

    def test_max_turns_cap_with_no_satisfying_call_grades_to_fail(self, tmp_path: Path) -> None:
        # The matchers still decide: a capped trajectory that does NOT satisfy the
        # positive matcher grades to FAIL — not a vacuous PASS.
        from teatree.eval.report import evaluate  # noqa: PLC0415

        spec = _spec(tmp_path)
        messages = [
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Bash", input={"command": "echo no worktree here"})],
                model="haiku",
            ),
        ]
        query = _yield_then_raise_query(
            messages, "Claude Code returned an error result: Reached maximum number of turns (3)"
        )
        run = self._run_with_query(spec, query)
        result = evaluate(spec, run)
        assert result.verdict == "fail"

    def test_max_turns_cap_with_no_captured_messages_falls_back_to_terminal_shape(self, tmp_path: Path) -> None:
        # When nothing was captured before the cap, fall back to the empty
        # terminal shape: is_error=True, terminal_reason=max_turns, no tool calls.
        spec = _spec(tmp_path)
        query = _yield_then_raise_query([], "Claude Code returned an error result: Reached maximum number of turns (3)")
        run = self._run_with_query(spec, query)
        assert run.terminal_reason == "max_turns"
        assert run.is_error is True
        assert run.tool_calls == ()

    def test_max_turns_cap_recovers_cost_from_captured_result_message(self, tmp_path: Path) -> None:
        # max-turns carries no "($X)" in the message; cost comes from a captured
        # ResultMessage if the SDK emitted one before the cap.
        spec = _spec(tmp_path)
        messages = [
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Bash", input={"command": "git worktree add ../wt HEAD"})],
                model="haiku",
            ),
            _result(total_cost_usd=0.0789),
        ]
        query = _yield_then_raise_query(
            messages, "Claude Code returned an error result: Reached maximum number of turns (3)"
        )
        run = self._run_with_query(spec, query)
        assert run.cost_usd == pytest.approx(0.0789)

    def test_max_turns_cap_without_result_message_reports_zero_cost(self, tmp_path: Path) -> None:
        # No captured ResultMessage and no amount in the max-turns message -> cost
        # 0.0, with terminal_reason making the incompleteness visible.
        spec = _spec(tmp_path)
        messages = [
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Bash", input={"command": "git worktree add ../wt HEAD"})],
                model="haiku",
            ),
        ]
        query = _yield_then_raise_query(
            messages, "Claude Code returned an error result: Reached maximum number of turns (3)"
        )
        run = self._run_with_query(spec, query)
        assert run.cost_usd == pytest.approx(0.0)

    def test_budget_cap_with_captured_trajectory_keeps_the_tool_call(self, tmp_path: Path) -> None:
        # Generalization regression: the budget path now ALSO captures a partial
        # trajectory when one was emitted before the cap (was discarded before).
        spec = _spec(tmp_path)
        messages = [
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Bash", input={"command": "git worktree add ../wt HEAD"})],
                model="haiku",
            ),
        ]
        query = _yield_then_raise_query(messages, "Claude Code returned an error result: Reached maximum budget ($0.1)")
        run = self._run_with_query(spec, query)
        assert run.terminal_reason == "budget_exceeded"
        assert len(run.tool_calls) == 1
        assert run.is_error is False
        # The budget amount in the message is still the recovered cost floor.
        assert run.cost_usd == pytest.approx(0.1)

    def test_non_terminal_error_after_partial_messages_still_propagates(self, tmp_path: Path) -> None:
        # Anti-vacuity for the partial path: a genuine error mid-stream (after some
        # captured messages) is NOT classified as terminal, so it re-raises — a
        # swallowed crash would grade a broken run as a real measurement.
        spec = _spec(tmp_path)
        messages = [
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})],
                model="haiku",
            ),
        ]
        query = _yield_then_raise_query(messages, "Claude Code returned an error result: error_during_execution")
        with pytest.raises(Exception, match="error_during_execution"):
            self._run_with_query(spec, query)
