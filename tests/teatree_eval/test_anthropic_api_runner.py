"""The CLI-free ``anthropic_api`` eval backend runs Claude without spawning the CLI.

The behavioral eval lane must be adoptable by a downstream harness that forbids the
Claude Code CLI (#3222), so a Claude model is graded through the Anthropic Messages
API DIRECTLY. These tests drive the runner with pydantic_ai's own model doubles
(`FunctionModel` / `TestModel`), so they run with no network, no key, and zero
tokens; the direct-API transport itself is proved by building the real
`AnthropicModel` from a fixed key (no `claude` binary, no network).
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from django.test import TestCase
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel

from teatree.eval.anthropic_api_runner import (
    AnthropicApiKeyMissingError,
    AnthropicApiRunner,
    build_anthropic_api_eval_runner,
)
from teatree.eval.backends import ANTHROPIC_API_BACKEND, KNOWN_BACKENDS, UnknownBackendError, make_runner
from teatree.eval.models import EvalSpec, Matcher
from teatree.eval.report import evaluate
from teatree.llm.credentials import AnthropicApiKeyCredential, CredentialSpec


class _FixedSource:
    """A credential source that always yields *value* (or ``None`` for absent)."""

    def __init__(self, value: str | None) -> None:
        self._value = value

    def lookup(self, _spec: CredentialSpec) -> str | None:
        return self._value


def _credential_with_key(value: str | None) -> AnthropicApiKeyCredential:
    return AnthropicApiKeyCredential(sources=(_FixedSource(value),))


def _spec(matcher: Matcher, *, tools: tuple[str, ...] = ("Bash",)) -> EvalSpec:
    return EvalSpec(
        name="cli_free_scenario",
        scenario="the agent runs the tests",
        agent_path="skills/code/SKILL.md",
        prompt="run the tests",
        matchers=(matcher,),
        source_path=Path("/tmp/spec.yaml"),
        # An explicit pin keeps the resolver DB-free; the model is injected anyway.
        model="claude-sonnet-5",
        tools=tools,
    )


def _tool_call_then_text(command: str, text: str) -> FunctionModel:
    """A streaming FunctionModel that issues one Bash tool call, then finishes with text."""
    state = {"turn": 0}

    async def stream_fn(_messages: object, _info: AgentInfo) -> AsyncIterator[object]:
        await asyncio.sleep(0)
        state["turn"] += 1
        if state["turn"] == 1:
            yield {0: DeltaToolCall(name="Bash", json_args=f'{{"command": "{command}"}}')}
        else:
            yield text

    return FunctionModel(stream_function=stream_fn)


class TestBackendSelection:
    def test_anthropic_api_is_a_known_backend(self) -> None:
        assert ANTHROPIC_API_BACKEND in KNOWN_BACKENDS

    def test_unknown_backend_still_raises(self) -> None:
        # The new branch did not swallow the unknown-backend guard.
        with pytest.raises(UnknownBackendError):
            make_runner("no-such-backend")


class TestClaudeScenarioRunsGreenWithoutTheCli:
    def test_a_tool_call_scenario_grades_green(self) -> None:
        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="pytest"))
        runner = AnthropicApiRunner(model=_tool_call_then_text("uv run pytest", "the tests pass"))
        result = evaluate(spec, runner.run(spec))
        assert result.passed, result.run.terminal_reason
        assert result.verdict == "pass"

    def test_the_tool_call_the_model_issued_is_captured(self) -> None:
        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="status"))
        runner = AnthropicApiRunner(model=_tool_call_then_text("git status", "clean"))
        run = runner.run(spec)
        assert [(c.name, c.input.get("command")) for c in run.tool_calls] == [("Bash", "git status")]
        assert run.terminal_reason == "success"
        assert run.is_error is False

    def test_a_negative_matcher_still_has_teeth(self) -> None:
        # A scenario forbidding a Write must FAIL when the model issues one — the
        # CLI-free lane grades negatives with full teeth, not a vacuous green.
        state = {"turn": 0}

        async def stream_fn(_messages: object, _info: AgentInfo) -> AsyncIterator[object]:
            await asyncio.sleep(0)
            state["turn"] += 1
            if state["turn"] == 1:
                yield {0: DeltaToolCall(name="Write", json_args='{"file_path": "x.py", "content": "boom"}')}
            else:
                yield "wrote the file"

        spec = _spec(
            Matcher(kind="negative", tool="Write", arg_path="file_path", operator="~", value=r".*\.py"),
            tools=("Bash", "Write"),
        )
        runner = AnthropicApiRunner(model=FunctionModel(stream_function=stream_fn))
        assert not evaluate(spec, runner.run(spec)).passed

    def test_a_text_only_model_produces_graded_text(self) -> None:
        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"))
        runner = AnthropicApiRunner(model=TestModel(custom_output_text="I considered the task"))
        run = runner.run(spec)
        assert run.text_blocks == ("I considered the task",)
        assert run.terminal_reason == "success"


class TestTransportIsTheAnthropicApiNotTheCli:
    def test_a_real_run_builds_a_direct_anthropic_model(self) -> None:
        # With a resolvable key and NO injected double, the runner builds a real
        # pydantic_ai AnthropicModel — the Anthropic Messages API transport, which
        # talks to api.anthropic.com directly and spawns no `claude` binary. Building
        # the model makes no network call, so this proves the transport with no token.
        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="."))
        runner = AnthropicApiRunner(credential=_credential_with_key("sk-ant-test"))
        model = runner._resolve_model_or_skip(spec)
        assert isinstance(model, AnthropicModel)
        assert model.model_name == "claude-sonnet-5"


class TestMissingKeyGate:
    def test_a_missing_key_skips_when_the_all_skipped_gate_is_disarmed(self) -> None:
        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="."))
        runner = AnthropicApiRunner(credential=_credential_with_key(None))
        run = runner.run(spec)
        assert run.terminal_reason.startswith("skipped:")
        assert run.is_error is False
        assert run.tool_calls == ()

    def test_a_missing_key_fails_loud_under_require_executed(self) -> None:
        # `require_executed` cannot tolerate a decorative all-skipped green: a
        # missing key raises on the FIRST scenario, the earliest fail-loud point.
        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="."))
        runner = AnthropicApiRunner(credential=_credential_with_key(None), require_executed=True)
        with pytest.raises(AnthropicApiKeyMissingError):
            runner.run(spec)


class TestRunnerWithSettings(TestCase):
    def test_make_runner_builds_the_anthropic_api_runner(self) -> None:
        runner = make_runner(ANTHROPIC_API_BACKEND)
        assert isinstance(runner, AnthropicApiRunner)

    def test_the_build_factory_threads_require_executed(self) -> None:
        runner = build_anthropic_api_eval_runner(require_executed=True)
        assert runner._require_executed is True
