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
from teatree.eval.sdk_runner import MAX_BUDGET_USD, ClaudeCliMissingError, SdkInProcessRunner


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
