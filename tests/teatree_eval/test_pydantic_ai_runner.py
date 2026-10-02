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
from typing import Any
from unittest.mock import patch

import httpx2
import pytest
from claude_agent_sdk.types import EffortLevel
from django.test import TestCase
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse, override_allow_model_requests
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext

from teatree.agents.model_aliases import family_alias_models
from teatree.agents.model_tiering import TIER_MODELS
from teatree.agents.pydantic_ai_config import LANE_EVAL, OpenAICompatibleLaneConfig
from teatree.agents.pydantic_ai_turn import SessionRun
from teatree.config.settings import PYDANTIC_AI_MAX_TOKENS_DEFAULT
from teatree.core.models import ConfigSetting
from teatree.eval.api_errors import THROTTLE_TERMINAL_PREFIX
from teatree.eval.backends import KNOWN_BACKENDS, PYDANTIC_AI_BACKEND, UnknownBackendError, make_runner
from teatree.eval.discovery import SCENARIOS_DIR, discover_specs
from teatree.eval.loader import load_eval_yaml
from teatree.eval.model_resolution import resolve_eval_model
from teatree.eval.models import CLEAN_ROOM_MIN_TURNS, EvalSpec, Matcher
from teatree.eval.pydantic_ai_runner import (
    EvalDriveCaps,
    PydanticAiRunner,
    build_eval_toolset,
    build_pydantic_ai_eval_runner,
)
from teatree.eval.report import evaluate
from teatree.eval.throttle_retry import ThrottleRetryDriver
from tests.teatree_agents._router_fake import text_reply


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


_ROUTER_HEADERS = {"X-OrcaRouter-Include-Cost": "true", "X-OrcaRouter-Session-Id": "t3-{session}"}


class TestRunnerWithSettings(TestCase):
    """The two paths that read DB-home settings: the factory and the real model build."""

    @pytest.fixture(autouse=True)
    def _backend_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://backend.example.invalid/v1")
        monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "dummy-backend-test-value")

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
        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="."))
        spec = dataclasses.replace(spec, model="claude-opus-4-8")
        runner = PydanticAiRunner(backend=OpenAICompatibleLaneConfig(lane=LANE_EVAL, model="vendor/some-model"))
        model = runner._resolve_model(spec, SessionRun.start())
        assert isinstance(model, OpenAIChatModel)
        # The abstract Claude id normalises UP to the CONFIGURED model id.
        assert model.model_name == "vendor/some-model"

    def test_the_eval_client_sends_the_allowlisted_router_headers_and_nothing_else(self) -> None:
        ConfigSetting.objects.create(
            key="openai_compatible_extra_headers", scope="", value={**_ROUTER_HEADERS, "XAuthToken": "canary-value"}
        )
        ConfigSetting.objects.set_value("openai_compatible_model", "vendor/some-model")
        runner = build_pydantic_ai_eval_runner()
        sent: list[httpx2.Request] = []

        def network(request: httpx2.Request) -> httpx2.Response:
            sent.append(request)
            return text_reply("done", cost_usd=0.0001)

        # The transport is faked, so a model request cannot leave the process whatever the global guard says.
        with (
            patch.object(
                httpx2.AsyncHTTPTransport, "handle_async_request", httpx2.MockTransport(network).handle_async_request
            ),
            override_allow_model_requests(allow_model_requests=True),
        ):
            runner.run(_spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value=".")))

        assert sent, "the eval lane never reached the endpoint"
        headers = sent[0].headers
        assert headers["X-OrcaRouter-Include-Cost"] == "true"
        assert headers["X-OrcaRouter-Session-Id"].startswith("t3-")
        assert "{session}" not in headers["X-OrcaRouter-Session-Id"]
        assert headers["x-lane"] == LANE_EVAL
        assert "XAuthToken" not in headers


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
        # A NON-throttle provider refusal is a genuine red, graded and never retried —
        # the anti-cheat boundary the throttle envelope below must not cross.
        async def stream_fn(_messages: object, _info: AgentInfo) -> AsyncIterator[str]:
            await asyncio.sleep(0)
            raise ModelHTTPError(
                status_code=400,
                model_name="claude-sonnet-5",
                body={"type": "error", "error": {"type": "invalid_request_error", "message": "bad prompt"}},
            )
            yield ""  # unreachable — the ``yield`` is what makes this an async GENERATOR

        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"))
        run = PydanticAiRunner(model=FunctionModel(stream_function=stream_fn)).run(spec)

        assert run.is_error is True
        assert run.terminal_reason == "error_during_execution"
        assert not evaluate(spec, run).passed, "a run that never happened must never grade green"


class TestProviderThrottleIsRiddenOutNotGradedAsBehavior:
    """A provider capacity error is NOT a behavioral fail.

    The metered lanes all run `--backend anthropic_api`, and this lane had no throttle
    envelope: a 529 `overloaded_error` folded straight into an `error_during_execution`
    red. `--escalate-on-fail` then re-ran it three times within seconds, hit the same
    overload window, and reported `CONFIRMED (0/3 escalation trials)` — the exact
    signature of a hard behavioral regression, produced by provider capacity.
    """

    @staticmethod
    def _throttling_model(*, fail_times: int, status: int, error_type: str) -> FunctionModel:
        state = {"calls": 0}

        async def stream_fn(_messages: object, _info: AgentInfo) -> AsyncIterator[object]:
            await asyncio.sleep(0)
            state["calls"] += 1
            if state["calls"] <= fail_times:
                raise ModelHTTPError(
                    status_code=status,
                    model_name="claude-sonnet-5",
                    body={"type": "error", "error": {"type": error_type, "message": error_type}},
                )
            if state["calls"] == fail_times + 1:
                yield {0: DeltaToolCall(name="Bash", json_args='{"command": "git push"}')}
            else:
                yield "pushed"

        return FunctionModel(stream_function=stream_fn)

    @staticmethod
    def _instant_retry(attempts: int) -> ThrottleRetryDriver:
        return ThrottleRetryDriver(max_attempts=attempts, timeout_max_attempts=0, sleep=lambda _s: None)

    @pytest.mark.parametrize(("status", "error_type"), [(529, "overloaded_error"), (429, "rate_limit_error")])
    def test_a_transient_throttle_is_retried_and_the_scenario_grades_on_the_real_run(
        self, status: int, error_type: str
    ) -> None:
        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="git push"))
        run = PydanticAiRunner(
            model=self._throttling_model(fail_times=2, status=status, error_type=error_type),
            retry=self._instant_retry(3),
        ).run(spec)

        assert run.is_error is False
        assert evaluate(spec, run).passed, "the retried attempt is the one that must be graded"

    def test_an_exhausted_throttle_surfaces_as_throttled_never_as_a_behavioral_fail(self) -> None:
        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="git push"))
        run = PydanticAiRunner(
            model=self._throttling_model(fail_times=99, status=529, error_type="overloaded_error"),
            retry=self._instant_retry(2),
        ).run(spec)

        assert run.is_error is True
        assert run.terminal_reason.startswith(THROTTLE_TERMINAL_PREFIX), run.terminal_reason
        assert run.terminal_reason != "error_during_execution"
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


class TestAnErroredTurnReportsEachToolCallOnce:
    """The error path recovers the trajectory from the event stream, without duplicating it.

    A run that RAISES returns no result, so the finished-history read has nothing to read
    and the streamed capture is the only record of what the model did. pydantic_ai surfaces
    ONE `ToolCallPart` through THREE events, so a capture keyed on the part type records
    every call three times — which reads as a model that blew its turn budget 3x over and
    reds a correctly-behaving agent on a turn-count matcher.
    """

    _MATCHER = Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value=".")

    def test_each_request_contributes_exactly_one_captured_call(self) -> None:
        spec = dataclasses.replace(_spec(self._MATCHER), max_turns=CLEAN_ROOM_MIN_TURNS)
        run = PydanticAiRunner(
            model=_tool_calls_then_text(tool_turns=CLEAN_ROOM_MIN_TURNS),
            backend=OpenAICompatibleLaneConfig(lane=LANE_EVAL, request_limit=2),
        ).run(spec)

        assert run.terminal_reason == "error_max_turns"
        assert len(run.tool_calls) == 2, "one captured call per request the model actually made"


class _RecordingAnthropicModel(AnthropicModel):
    """A REAL ``AnthropicModel`` whose requests are served offline, recording their settings.

    Mirrors :class:`_RecordingOpenAIModel` on the Anthropic branch: the settings CLASS
    is chosen from the model's provider discriminator, so only a real ``AnthropicModel``
    exercises the branch that builds ``anthropic_effort``.
    """

    def __init__(self, model_name: str, offline: Model) -> None:
        super().__init__(model_name, provider=AnthropicProvider(api_key="offline-double"))
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


class TestEffortIsGatedOnTheModelsCapability:
    """A model that carries no effort lever is sent none -- the whole request is at stake.

    Haiku answers ANY request carrying an effort with ``400 invalid_request_error: This
    model does not support the effort parameter``. That is a whole-request rejection, so
    the run captures an EMPTY trajectory and the scenario reds as
    ``error_during_execution`` having graded nothing. Measured on the real API: the
    ``--preset baseline`` lane pins its cheapest-passing scenarios to the ``cheap``/Haiku
    tier while the lane-level ``METERED_DEFAULT_EFFORT`` rides on every scenario, so 22
    of 266 baseline scenarios could never execute. The vocabulary guard cannot catch it --
    ``high`` is a perfectly valid rung; the MODEL is what lacks the lever.
    """

    def _recorded_settings(self, model_name: str, *, effort: EffortLevel | None) -> ModelSettings:
        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="."))
        model = _RecordingAnthropicModel(model_name, _tool_call_then_text("uv run pytest", "done"))
        PydanticAiRunner(model=model, caps=EvalDriveCaps(effort=effort)).run(spec)
        assert model.recorded, "the model was never asked for a request"
        settings = model.recorded[0]
        assert settings is not None
        return settings

    def test_the_cheap_haiku_tier_is_sent_no_effort(self) -> None:
        assert "anthropic_effort" not in self._recorded_settings(TIER_MODELS["cheap"], effort="high")

    def test_a_reasoning_tier_still_carries_the_lane_effort(self) -> None:
        settings = self._recorded_settings(TIER_MODELS["balanced"], effort="high")
        assert settings.get("anthropic_effort") == "high"

    def test_the_output_ceiling_rides_even_with_the_effort_dropped(self) -> None:
        settings = self._recorded_settings(TIER_MODELS["cheap"], effort="high")
        assert settings.get("max_tokens") == PYDANTIC_AI_MAX_TOKENS_DEFAULT


class TestEveryScenarioResolvesToAConcreteModelId:
    """No catalog scenario may resolve to a bare FAMILY alias.

    ``model: haiku`` is what the Claude CLI accepts, and the ``api`` backend forwards it
    to that CLI which resolves the family itself. The ``anthropic_api`` backend talks to
    the Messages API directly, which knows no families: it answers ``404 not_found_error:
    model: haiku`` and the scenario reds having executed nothing. Two catalog scenarios
    carried such a pin and so had never once run on the backend the baseline lane uses.

    Both now declare ``tier: cheap`` instead. Naming the concrete id would satisfy this
    assertion and trip the sibling ratchet in ``tests/quality/test_no_hardcoded_model_ids``:
    an abstract tier is the one spelling that satisfies both, because it resolves through
    ``TIER_MODELS`` at run time and carries a model bump with it.
    """

    def test_no_discovered_spec_pins_a_family_alias(self) -> None:
        aliases = set(family_alias_models())
        assert aliases, "the family-alias map is empty -- this assertion would be vacuous"
        offenders = {
            spec.name: resolve_eval_model(spec) for spec in discover_specs() if resolve_eval_model(spec) in aliases
        }
        assert not offenders, (
            f"these scenarios pin a family alias the Messages API cannot resolve: {offenders}. "
            "Name the concrete catalog id (TIER_MODELS) or declare an abstract tier instead."
        )
