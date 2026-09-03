"""The `pydantic_ai` eval backend runs a NON-Claude model green.

The behavioral eval lane must be able to grade a non-Claude model so a GPT/OSS swap
is verifiable. These tests drive the runner with pydantic_ai's own model doubles
(`FunctionModel` / `TestModel`) under `ALLOW_MODEL_REQUESTS=False`, so they run with
no network, no the OpenAI-compatible backend credential, and zero tokens.
"""

import asyncio
import dataclasses
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk.types import EffortLevel
from django.test import TestCase
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext

from teatree.agents.pydantic_ai_config import LANE_EVAL, OpenAICompatibleLaneConfig
from teatree.config.settings import PYDANTIC_AI_MAX_TOKENS_DEFAULT
from teatree.core.models import ConfigSetting
from teatree.eval.backends import KNOWN_BACKENDS, PYDANTIC_AI_BACKEND, UnknownBackendError, make_runner
from teatree.eval.discovery import SCENARIOS_DIR
from teatree.eval.loader import load_eval_yaml
from teatree.eval.models import CLEAN_ROOM_MIN_TURNS, EvalSpec, Matcher
from teatree.eval.pydantic_ai_runner import EvalDriveCaps, PydanticAiRunner, build_eval_toolset
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


def _catalog_specs() -> list[EvalSpec]:
    return [spec for path in sorted(SCENARIOS_DIR.glob("*.yaml")) for spec in load_eval_yaml(path)]


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


class _RecordingOpenAIModel(OpenAIChatModel):
    """A REAL ``OpenAIChatModel`` whose requests are served offline, recording their settings.

    Subclassing the real provider model keeps the runner's provider branch honest —
    the settings class is chosen from the model, so a stand-in would grade the test's
    own guess. Requests go to *offline*, so no key and no network are used.
    """

    def __init__(self, offline: Model) -> None:
        super().__init__("gpt-5", provider=OpenAIProvider(api_key="offline-double"))
        self._offline = offline
        self.recorded: list[ModelSettings | None] = []

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[None] | None = None,
    ) -> AsyncIterator[StreamedResponse]:
        self.recorded.append(model_settings)
        async with self._offline.request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as response:
            yield response


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
        # A `model@effort` pin must reach the model under the key an OpenAI-compatible
        # provider reads — asserted on the settings the model was handed, since a run
        # that merely finishes proves nothing about a setting the provider ignores.
        spec = EvalSpec(
            name="effort_scenario",
            scenario="run with high effort",
            agent_path="skills/code/SKILL.md",
            prompt="think hard",
            matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="."),),
            source_path=Path("/tmp/spec.yaml"),
            model="claude-sonnet-5@high",
        )
        model = _RecordingOpenAIModel(_tool_call_then_text("uv run pytest", "done"))
        run = PydanticAiRunner(model=model).run(spec)
        assert run.terminal_reason == "success"
        assert model.recorded, "the model was never asked for a request"
        assert model.recorded[0] is not None
        assert model.recorded[0].get("openai_reasoning_effort") == "high"
        assert "anthropic_effort" not in model.recorded[0]


class TestRunnerWithSettings(TestCase):
    """The two paths that read DB-home settings: the factory and the real model build."""

    def test_make_runner_builds_the_pydantic_ai_runner_on_the_eval_lane(self) -> None:
        runner = make_runner(PYDANTIC_AI_BACKEND)
        assert isinstance(runner, PydanticAiRunner)
        # The eval runner tags its the OpenAI-compatible backend dispatch with the `eval` x-lane header.
        assert runner._backend.lane == LANE_EVAL

    def test_make_runner_threads_the_configured_output_ceiling(self) -> None:
        ConfigSetting.objects.set_value("pydantic_ai_max_tokens", value=24576)
        runner = make_runner(PYDANTIC_AI_BACKEND)
        assert isinstance(runner, PydanticAiRunner)
        assert runner._caps.max_tokens == 24576

    def test_resolve_model_builds_the_configured_model_on_the_eval_lane(self) -> None:
        # With no injected model, `_resolve_model` builds a real OpenAI-compatible
        # model — mocked at the credential boundary so the test needs no live key
        # or network.
        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="."))
        spec = dataclasses.replace(spec, model="claude-opus-4-8")
        runner = PydanticAiRunner(
            backend=OpenAICompatibleLaneConfig(
                lane=LANE_EVAL, base_url="https://backend.example/v1", model="vendor/some-model"
            )
        )
        with patch(
            "teatree.eval.pydantic_ai_runner.resolve_openai_compatible_backend",
            lambda **_: SimpleNamespace(base_url="https://backend.example/v1", api_key="k"),
        ):
            model = runner._resolve_model(spec)
        assert isinstance(model, OpenAIChatModel)
        # The abstract Claude id normalises UP to the CONFIGURED model id.
        assert model.model_name == "vendor/some-model"


class TestOutputCeilingOnTheRouterLane:
    """``max_tokens`` is a base settings key both bindings honour — it rides here too.

    The Anthropic instruction-cache key is NOT: it is Anthropic-namespaced, and a
    foreign key on an OpenAI-compatible request is at best ignored and at worst
    rejected by the endpoint.
    """

    def _recorded_settings(self, *, effort: EffortLevel | None = None) -> ModelSettings:
        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="."))
        model = _RecordingOpenAIModel(_tool_call_then_text("uv run pytest", "done"))
        PydanticAiRunner(model=model, caps=EvalDriveCaps(effort=effort)).run(spec)
        assert model.recorded, "the model was never asked for a request"
        settings = model.recorded[0]
        assert settings is not None
        return settings

    def test_an_output_ceiling_is_always_sent(self) -> None:
        assert self._recorded_settings().get("max_tokens") == PYDANTIC_AI_MAX_TOKENS_DEFAULT

    def test_the_anthropic_cache_key_never_reaches_the_router(self) -> None:
        assert "anthropic_cache_instructions" not in self._recorded_settings(effort="high")

    def test_the_ceiling_rides_alongside_the_openai_effort(self) -> None:
        settings = self._recorded_settings(effort="high")
        assert settings.get("max_tokens") == PYDANTIC_AI_MAX_TOKENS_DEFAULT
        assert settings.get("openai_reasoning_effort") == "high"


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

    @staticmethod
    def _advertised(name: str) -> dict[str, Any]:
        return build_eval_toolset((name,)).tools[name].function_schema.json_schema

    def test_a_structured_tool_advertises_the_parameters_it_really_takes(self) -> None:
        # A stub whose only parameter is `**kwargs` advertises ZERO properties, so the
        # model is never told what `AskUserQuestion` takes and emits `AskUserQuestion({})`
        # — every `args.questions` matcher then reds a correctly-behaving agent.
        assert "questions" in self._advertised("AskUserQuestion")["properties"]

    def test_an_unmodelled_tool_still_registers_permissively(self) -> None:
        schema = self._advertised("Frobnicate")
        assert schema["properties"] == {}
        assert schema["additionalProperties"] is True

    def test_every_tool_the_catalog_declares_carries_a_schema(self) -> None:
        declared = {tool for spec in _catalog_specs() for tool in spec.tools}
        assert {t for t in declared if not self._advertised(t)["properties"]} == set()

    def test_an_argument_outside_the_advertised_schema_still_flows_through(self) -> None:
        # `additionalProperties: true` is what keeps the schema a HINT, not a filter:
        # a key the curated shape does not model must still reach the captured call.
        state = {"turn": 0}

        async def stream_fn(_messages: object, _info: AgentInfo) -> AsyncIterator[object]:
            await asyncio.sleep(0)
            state["turn"] += 1
            if state["turn"] == 1:
                yield {0: DeltaToolCall(name="Bash", json_args='{"command": "ls", "undeclared": 1}')}
            else:
                yield "listed"

        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="ls"))
        run = PydanticAiRunner(model=FunctionModel(stream_function=stream_fn)).run(spec)
        assert run.tool_calls[0].input == {"command": "ls", "undeclared": 1}


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


class TestProviderFailureFoldsIntoTheRun:
    """A provider error is a GRADED error run, not a crash out of the scenario.

    ``PydanticAiRunner._drive`` reuses ``PydanticAiHarnessSession``, so the session's
    failure mapping reaches this lane too: a throttled or refused request now ends the
    scenario as an ``is_error`` run the report can record, where it previously escaped
    ``asyncio.run`` and took the whole scenario down with a traceback.
    """

    def test_a_refused_request_ends_the_scenario_as_an_error_run(self) -> None:
        async def stream_fn(_messages: object, _info: AgentInfo) -> AsyncIterator[str]:
            await asyncio.sleep(0)
            raise ModelHTTPError(
                status_code=429,
                model_name="claude-sonnet-5",
                body={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
            )
            yield ""  # unreachable — the ``yield`` is what makes this an async GENERATOR

        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"))
        run = PydanticAiRunner(model=FunctionModel(stream_function=stream_fn)).run(spec)

        assert run.is_error is True
        assert run.terminal_reason == "error_during_execution"
        assert not evaluate(spec, run).passed, "a run that never happened must never grade green"


def _tool_calls_then_text(*, tool_turns: int) -> FunctionModel:
    """A model that issues one Bash call per turn for *tool_turns* turns, then finishes."""
    state = {"turn": 0}

    async def stream_fn(_messages: object, _info: AgentInfo) -> AsyncIterator[object]:
        await asyncio.sleep(0)
        state["turn"] += 1
        if state["turn"] <= tool_turns:
            yield {0: DeltaToolCall(name="Bash", json_args='{"command": "echo hi"}')}
        else:
            yield "done"

    return FunctionModel(stream_function=stream_fn)


class TestScenarioCapsBindThisLane:
    """A scenario's own ``max_turns`` / ``watchdog_seconds`` cap this lane, as they do the SDK lane."""

    _MATCHER = Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value=".")

    def test_the_scenario_turn_budget_caps_the_request_loop(self) -> None:
        spec = dataclasses.replace(_spec(self._MATCHER), max_turns=CLEAN_ROOM_MIN_TURNS)
        run = PydanticAiRunner(model=_tool_calls_then_text(tool_turns=CLEAN_ROOM_MIN_TURNS + 5)).run(spec)
        assert run.terminal_reason == "error_max_turns"
        assert len(run.tool_calls) <= CLEAN_ROOM_MIN_TURNS

    def test_a_tight_clean_room_budget_is_floored_like_the_sdk_lane(self) -> None:
        # Parity guard, not a regression: many catalog scenarios declare `max_turns: 3`,
        # so honouring the raw declaration would red every one of them on this lane.
        spec = dataclasses.replace(_spec(self._MATCHER), max_turns=3)
        run = PydanticAiRunner(model=_tool_calls_then_text(tool_turns=5)).run(spec)
        assert run.terminal_reason != "error_max_turns"
        assert len(run.tool_calls) == 5

    def test_the_lane_request_guardrail_still_binds_when_tighter(self) -> None:
        spec = dataclasses.replace(_spec(self._MATCHER), max_turns=CLEAN_ROOM_MIN_TURNS)
        runner = PydanticAiRunner(
            model=_tool_calls_then_text(tool_turns=CLEAN_ROOM_MIN_TURNS),
            backend=OpenAICompatibleLaneConfig(lane=LANE_EVAL, request_limit=2),
        )
        run = runner.run(spec)
        assert run.terminal_reason == "error_max_turns"
        assert len(run.tool_calls) <= 2

    def test_the_scenario_watchdog_overrides_the_lane_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.eval.pydantic_ai_runner.resolve_watchdog_seconds", lambda: 30.0)

        async def stream_fn(_messages: object, _info: AgentInfo) -> AsyncIterator[object]:
            await asyncio.sleep(1.0)
            yield "too late"

        spec = dataclasses.replace(_spec(self._MATCHER), watchdog_seconds=0.05)
        run = PydanticAiRunner(model=FunctionModel(stream_function=stream_fn)).run(spec)
        assert run.is_error is True
        assert run.terminal_reason == "timeout"
