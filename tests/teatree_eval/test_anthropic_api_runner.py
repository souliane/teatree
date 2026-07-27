"""The CLI-free ``anthropic_api`` eval backend runs Claude without spawning the CLI.

The behavioral eval lane must be adoptable by a downstream harness that forbids the
Claude Code CLI (#3222), so a Claude model is graded through the Anthropic Messages
API DIRECTLY. These tests drive the runner with pydantic_ai's own model doubles
(`FunctionModel` / `TestModel`), so they run with no network, no key, and zero
tokens; the direct-API transport itself is proved by building the real
`AnthropicModel` from a fixed key (no `claude` binary, no network).
"""

import asyncio
import dataclasses
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from claude_agent_sdk.types import EffortLevel
from django.test import TestCase
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext

from teatree.config.settings import PYDANTIC_AI_MAX_TOKENS_DEFAULT
from teatree.core.models import ConfigSetting
from teatree.eval.anthropic_api_runner import (
    AnthropicApiKeyMissingError,
    AnthropicApiRunner,
    build_anthropic_api_eval_runner,
)
from teatree.eval.backends import ANTHROPIC_API_BACKEND, KNOWN_BACKENDS, UnknownBackendError, make_runner
from teatree.eval.models import EvalSpec, Matcher
from teatree.eval.pydantic_ai_runner import EVAL_CACHE_TTL, EvalDriveCaps
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


class _RecordingAnthropicModel(AnthropicModel):
    """A REAL ``AnthropicModel`` whose requests are served offline, recording their settings.

    Subclassing the real provider model is what gives this double its teeth: the runner
    picks the settings class from the model it was handed, so a stand-in that merely
    claims to be Anthropic would grade the test's own guess instead of the runner's
    branch. Every request is delegated to *offline*, so no key and no network are used.
    """

    def __init__(self, offline: Model) -> None:
        super().__init__("claude-sonnet-5", provider=AnthropicProvider(api_key="offline-double"))
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


class TestReasoningEffortReachesTheAnthropicModel:
    """The resolved effort must land under the key ``AnthropicModel`` actually reads.

    ``AnthropicModel`` consults ``anthropic_effort``; ``openai_reasoning_effort`` is
    not in its settings vocabulary, so an OpenAI-keyed effort is accepted, resolved,
    passed, and silently discarded — the lane runs at the provider default while the
    report claims the pinned effort. These assertions read the settings the model was
    handed, so a "no exception" green cannot stand in for a delivered effort.
    """

    def _recorded_settings(self, *, spec: EvalSpec, effort: str | None) -> ModelSettings:
        model = _RecordingAnthropicModel(_tool_call_then_text("uv run pytest", "done"))
        AnthropicApiRunner(model=model, caps=EvalDriveCaps(effort=effort)).run(spec)
        assert model.recorded, "the model was never asked for a request"
        settings = model.recorded[0]
        assert settings is not None
        return settings

    def test_the_lane_effort_lands_under_the_anthropic_key(self) -> None:
        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="."))
        settings = self._recorded_settings(spec=spec, effort="high")
        assert settings.get("anthropic_effort") == "high"

    def test_a_scenario_effort_pin_lands_under_the_anthropic_key(self) -> None:
        # A scenario's own `model@effort` pin wins over the lane default, and must
        # reach the model on the same key.
        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="."))
        spec = dataclasses.replace(spec, model="claude-sonnet-5@low")
        settings = self._recorded_settings(spec=spec, effort="high")
        assert settings.get("anthropic_effort") == "low"

    def test_the_openai_key_is_never_sent_to_an_anthropic_model(self) -> None:
        # The OpenAI-shaped key is not merely redundant here — it is the whole bug:
        # the model ignores it, so its presence would mean the effort never arrived.
        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="."))
        settings = self._recorded_settings(spec=spec, effort="high")
        assert "openai_reasoning_effort" not in settings


class TestOutputCeilingAndCachingReachTheAnthropicModel:
    """The graded envelope must not be capped at the binding's 4096 default.

    ``AnthropicModel`` reads ``max_tokens`` off the settings dict and falls back to
    4096 when it is absent, so an unset ceiling truncates a long result envelope
    mid-JSON — on the one lane whose whole job is grading those envelopes. The
    system prompt is byte-identical across every scenario sharing an ``agent_path``,
    so the same settings dict also carries the instruction-cache key.
    """

    def _recorded_settings(
        self, *, effort: EffortLevel | None = None, max_tokens: int = PYDANTIC_AI_MAX_TOKENS_DEFAULT
    ) -> ModelSettings:
        spec = _spec(Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="."))
        model = _RecordingAnthropicModel(_tool_call_then_text("uv run pytest", "done"))
        caps = EvalDriveCaps(effort=effort, max_tokens=max_tokens)
        AnthropicApiRunner(model=model, caps=caps).run(spec)
        assert model.recorded, "the model was never asked for a request"
        settings = model.recorded[0]
        assert settings is not None
        return settings

    def test_an_output_ceiling_is_always_sent(self) -> None:
        settings = self._recorded_settings()
        assert settings.get("max_tokens") == PYDANTIC_AI_MAX_TOKENS_DEFAULT

    def test_an_explicit_ceiling_wins(self) -> None:
        settings = self._recorded_settings(max_tokens=32768)
        assert settings.get("max_tokens") == 32768

    def test_a_zero_ceiling_leaves_the_binding_default(self) -> None:
        # `0` is the documented escape hatch on `pydantic_ai_max_tokens`; it must not
        # reach the wire as a literal 0-token ceiling.
        settings = self._recorded_settings(max_tokens=0, effort="high")
        assert "max_tokens" not in settings

    def test_the_instruction_cache_key_is_sent(self) -> None:
        settings = self._recorded_settings()
        assert settings.get("anthropic_cache_instructions") == EVAL_CACHE_TTL

    def test_the_ceiling_rides_alongside_the_effort(self) -> None:
        settings = self._recorded_settings(effort="high")
        assert settings.get("max_tokens") == PYDANTIC_AI_MAX_TOKENS_DEFAULT
        assert settings.get("anthropic_effort") == "high"

    def test_no_openai_key_reaches_the_anthropic_model(self) -> None:
        settings = self._recorded_settings(effort="high")
        assert not [key for key in settings if key.startswith("openai_")]


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

    def test_the_build_factory_threads_the_configured_output_ceiling(self) -> None:
        ConfigSetting.objects.set_value("pydantic_ai_max_tokens", value=24576)
        assert build_anthropic_api_eval_runner()._caps.max_tokens == 24576
