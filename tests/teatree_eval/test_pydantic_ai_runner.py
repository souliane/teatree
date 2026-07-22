"""The `pydantic_ai` eval backend runs a NON-Claude model green.

The behavioral eval lane must be able to grade a non-Claude model so a GPT/OSS swap
is verifiable. These tests drive the runner with pydantic_ai's own model doubles
(`FunctionModel` / `TestModel`) under `ALLOW_MODEL_REQUESTS=False`, so they run with
no network, no OrcaRouter credential, and zero tokens.
"""

import asyncio
import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import TestCase
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.test import TestModel

from teatree.agents.pydantic_ai_config import LANE_EVAL
from teatree.eval.backends import KNOWN_BACKENDS, PYDANTIC_AI_BACKEND, UnknownBackendError, make_runner
from teatree.eval.models import EvalSpec, Matcher
from teatree.eval.pydantic_ai_runner import PydanticAiRunner
from teatree.eval.report import evaluate


def _spec(matcher: Matcher, *, tools: tuple[str, ...] = ("Bash",)) -> EvalSpec:
    return EvalSpec(
        name="oss_scenario",
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
    def test_pydantic_ai_is_a_known_backend(self) -> None:
        assert PYDANTIC_AI_BACKEND in KNOWN_BACKENDS

    def test_unknown_backend_still_raises(self) -> None:
        # The new branch did not swallow the unknown-backend guard.
        with pytest.raises(UnknownBackendError):
            make_runner("no-such-backend")


class TestNonClaudeScenarioRunsGreen:
    def test_a_tool_call_scenario_grades_green(self) -> None:
        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="pytest"))
        runner = PydanticAiRunner(model=_tool_call_then_text("uv run pytest", "the tests pass"))
        run = runner.run(spec)
        result = evaluate(spec, run)
        assert result.passed, result.run.terminal_reason
        assert result.verdict == "pass"

    def test_the_tool_call_the_model_issued_is_captured(self) -> None:
        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="status"))
        runner = PydanticAiRunner(model=_tool_call_then_text("git status", "clean"))
        run = runner.run(spec)
        assert [(c.name, c.input.get("command")) for c in run.tool_calls] == [("Bash", "git status")]
        assert run.terminal_reason == "success"
        assert run.is_error is False

    def test_a_negative_matcher_still_has_teeth(self) -> None:
        # A scenario forbidding a Write must FAIL when the model issues one — the
        # non-Claude lane grades negatives with full teeth, not a vacuous green.
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
        runner = PydanticAiRunner(model=FunctionModel(stream_function=stream_fn))
        result = evaluate(spec, runner.run(spec))
        assert not result.passed

    def test_a_provider_error_folds_into_the_run_not_a_scenario_crash(self) -> None:
        # A provider error (a 429) now surfaces as an is_error EvalRun — the seam
        # maps it to an is_error ResultMessage the runner collects — rather than
        # propagating out of ``runner.run`` and crashing the whole scenario
        # (RED: ``runner.run`` raised ModelHTTPError).
        exc = ModelHTTPError(status_code=429, model_name="m", body={"error": {"type": "rate_limit_error"}})

        async def stream_fn(_messages: object, _info: AgentInfo) -> AsyncIterator[object]:
            await asyncio.sleep(0)
            raise exc
            yield ""  # unreachable; marks stream_fn as an async generator

        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"))
        run = PydanticAiRunner(model=FunctionModel(stream_function=stream_fn)).run(spec)
        assert run.is_error is True

    def test_a_text_only_model_produces_graded_text(self) -> None:
        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"))
        runner = PydanticAiRunner(model=TestModel(custom_output_text="I considered the task"))
        run = runner.run(spec)
        assert run.text_blocks == ("I considered the task",)
        assert run.terminal_reason == "success"

    def test_a_scenario_effort_pin_is_carried_into_the_run(self) -> None:
        # A `model@effort` pin resolves to OpenAI reasoning-effort model settings
        # (dropped for a text/function double, but the effort-settings path is taken).
        spec = EvalSpec(
            name="effort_scenario",
            scenario="run with high effort",
            agent_path="skills/code/SKILL.md",
            prompt="think hard",
            matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="."),),
            source_path=Path("/tmp/spec.yaml"),
            model="claude-sonnet-5@high",
        )
        runner = PydanticAiRunner(model=_tool_call_then_text("uv run pytest", "done"))
        run = runner.run(spec)
        assert run.terminal_reason == "success"


class TestRunnerWithSettings(TestCase):
    """The two paths that read DB-home settings: the factory and the real model build."""

    def test_make_runner_builds_the_pydantic_ai_runner_on_the_eval_lane(self) -> None:
        runner = make_runner(PYDANTIC_AI_BACKEND)
        assert isinstance(runner, PydanticAiRunner)
        # The eval runner tags its OrcaRouter dispatch with the `eval` x-lane header.
        assert runner._orca.lane == LANE_EVAL

    def test_resolve_model_builds_the_orca_router_model_on_the_eval_lane(self) -> None:
        # With no injected model, `_resolve_model` builds a real OrcaRouter
        # OpenAI-compatible model — mocked at the credential boundary so the test
        # needs no live BYOK key or network.
        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="."))
        spec = dataclasses.replace(spec, model="claude-opus-4-8")
        with patch(
            "teatree.eval.pydantic_ai_runner.resolve_orca_router_provider_config",
            lambda **_: SimpleNamespace(base_url="https://orca.example/v1", api_key="k"),
        ):
            model = PydanticAiRunner()._resolve_model(spec)
        assert isinstance(model, OpenAIChatModel)
        # The abstract Claude id normalises UP to the OrcaRouter router handle.
        assert model.model_name == "orcarouter/teatree-factory"


class TestEvalToolset:
    def test_each_declared_tool_is_callable_and_captured(self) -> None:
        # `TestModel(call_tools='all')` calls every registered tool once — so a run
        # over a spec declaring three tools captures a call to each, proving the
        # inert toolset exposes exactly the scenario's declared tools.
        spec = _spec(
            Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="."),
            tools=("Bash", "Edit", "Read"),
        )
        runner = PydanticAiRunner(model=TestModel(call_tools="all"))
        run = runner.run(spec)
        assert {c.name for c in run.tool_calls} == {"Bash", "Edit", "Read"}


class TestWatchdog:
    def test_a_hang_yields_an_error_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A model that never terminates within the watchdog budget produces an
        # error-shaped run (is_error, terminal_reason="timeout"), not a hang.
        monkeypatch.setattr("teatree.eval.pydantic_ai_runner.resolve_watchdog_seconds", lambda: 0.05)

        async def stream_fn(_messages: object, _info: AgentInfo) -> AsyncIterator[object]:
            await asyncio.sleep(5)
            yield "too late"

        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"))
        runner = PydanticAiRunner(model=FunctionModel(stream_function=stream_fn))
        run = runner.run(spec)
        assert run.is_error is True
        assert run.terminal_reason == "timeout"
