"""The ``Harness`` seam — backend resolution + the provider-agnostic driver (#2565, #2885).

``resolve_harness`` reads the DB-home ``agent_harness`` setting and returns the
transport backend: the default resolves to :class:`ClaudeSdkHarness`
(byte-identical to the pre-seam transport), ``pydantic_ai`` resolves to
:class:`PydanticAiHarness` (#2885's the OpenAI-compatible backend-BYOK, OpenAI-compatible backend),
and the ``T3_AGENT_HARNESS`` env / ``ConfigSetting`` store are the switch.
``_drive_with_heartbeat`` talks only to the narrow ``HarnessSession`` surface, so
an arbitrary backend drives a run — both backends yield the SAME
``claude_agent_sdk`` message vocabulary, proved here for ``PydanticAiHarness`` the
same way :class:`FakeHarnessSession` proves it for the generic seam.
"""

import asyncio
import json
import os
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock
from django.test import TestCase
from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import ModelHTTPError, UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models import ModelRequestParameters, StreamedResponse
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.toolsets import FunctionToolset

import teatree.agents.harness as harness_mod
import teatree.agents.pydantic_ai_config as pyconfig_mod
import teatree.agents.runner as runner_mod
from teatree.agents import harness_registry
from teatree.agents.harness import (
    ClaudeSdkHarness,
    Harness,
    HarnessSession,
    PydanticAiHarness,
    PydanticAiHarnessSession,
    pydantic_ai_thread,
    resolve_dispatch_provider,
    resolve_effort,
    resolve_harness,
)
from teatree.agents.harness_options import HarnessOptions
from teatree.agents.harness_registry import InvalidHarnessProviderError, register_harness
from teatree.agents.model_tiering import UnconfiguredOpenAICompatibleModelError
from teatree.agents.pydantic_ai_config import (
    LANE_BULK,
    LANE_EVAL,
    LANE_FACTORY,
    OpenAICompatibleLaneConfig,
    PydanticAiModelConfig,
    build_openai_compatible_provider,
)
from teatree.agents.pydantic_ai_resume import persist_parked_thread
from teatree.agents.runner import LoopWatchdog, TaskUsage, _build_options, _drive_with_heartbeat, run_agent
from teatree.config import AgentHarnessProvider, get_effective_settings
from teatree.core.models import ConfigSetting, Session, Task, TaskAttempt, UsageWindowState
from teatree.llm.credentials import CredentialError
from teatree.llm.openai_compatible import OpenAICompatibleBackend
from tests.factories import planned_ticket
from tests.teatree_agents._sdk_fake import FakeHarness, FakeHarnessSession, assistant_text, result_message


def test_concrete_impls_satisfy_the_harness_protocols() -> None:
    # The Protocol-typed bindings are load-bearing, not decorative: they assert
    # conformance at type-check time — both backends ARE a Harness, the session
    # doubles ARE a HarnessSession — while the runtime asserts pin the seam's
    # methods across every backend.
    claude_harness: Harness = ClaudeSdkHarness()
    pydantic_harness: Harness = PydanticAiHarness()
    fake_session: HarnessSession = FakeHarnessSession([result_message(session_id="s1")])
    pydantic_session: HarnessSession = PydanticAiHarnessSession(Agent(TestModel()), model_name="test")

    assert callable(claude_harness.open)
    assert callable(pydantic_harness.open)
    for session in (fake_session, pydantic_session):
        assert callable(session.query)
        assert callable(session.receive_response)
        assert callable(session.interrupt)


def test_pydantic_ai_harness_open_enters_and_exits_the_agent() -> None:
    # ``Agent.__aenter__``/``__aexit__`` own the provider's HTTP client
    # lifecycle — a bare ``Agent(...)`` with no ``async with`` never closes it.
    # Assert the entered/exited transition directly since pydantic_ai exposes
    # no public "is the client closed" probe.
    harness = PydanticAiHarness(model=TestModel())
    options = ClaudeAgentOptions()

    async def drive() -> tuple[int, int]:
        async with harness.open(options) as session:
            assert isinstance(session, PydanticAiHarnessSession)
            entered_count_inside = session._agent._entered_count
        return entered_count_inside, session._agent._entered_count

    inside, after = asyncio.run(drive())
    assert inside == 1
    assert after == 0


def test_pydantic_ai_harness_open_seeds_the_session_with_injected_history() -> None:
    # (#2886) The harness-level `history` constructor param threads through
    # `open()` into the opened session, unchanged.
    seed_agent = Agent(TestModel(custom_output_text="seed"))
    seed_history = asyncio.run(seed_agent.run("seed")).all_messages()
    harness = PydanticAiHarness(model=TestModel(), history=seed_history)
    options = ClaudeAgentOptions()

    async def drive() -> list[ModelMessage]:
        async with harness.open(options) as session:
            assert isinstance(session, PydanticAiHarnessSession)
            return session.history

    assert asyncio.run(drive()) == seed_history


class TestResolveHarness(TestCase):
    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_AGENT_HARNESS", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_default_resolves_to_claude_sdk_backend(self) -> None:
        assert get_effective_settings().agent_harness.value == "claude_sdk"
        assert isinstance(resolve_harness(), ClaudeSdkHarness)

    def test_stored_claude_sdk_resolves_to_claude_sdk_backend(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "claude_sdk")
        assert isinstance(resolve_harness(), ClaudeSdkHarness)

    def test_stored_pydantic_ai_resolves_to_pydantic_ai_backend(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        # Resolving the backend never itself requires a live the OpenAI-compatible backend
        # credential — that resolves LAZILY inside PydanticAiHarness.open.
        assert isinstance(resolve_harness(), PydanticAiHarness)

    def test_env_switch_to_pydantic_ai_resolves_to_pydantic_ai_backend(self) -> None:
        # The env layer is the switch: it wins over the store.
        ConfigSetting.objects.set_value("agent_harness", "claude_sdk")
        with patch.dict(os.environ, {"T3_AGENT_HARNESS": "pydantic_ai"}):
            assert isinstance(resolve_harness(), PydanticAiHarness)

    def test_env_switch_back_to_claude_sdk_wins_over_stored_pydantic_ai(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        with patch.dict(os.environ, {"T3_AGENT_HARNESS": "claude_sdk"}):
            assert isinstance(resolve_harness(), ClaudeSdkHarness)


class TestResolveHarnessRehydratesPydanticAiThread(TestCase):
    """``resolve_harness(task)`` seeds the resumed harness with the parked thread (#2886)."""

    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_AGENT_HARNESS", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def setUp(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        self.ticket = planned_ticket()
        self.session = Session.objects.create(ticket=self.ticket)
        self.parked = Task.objects.create(ticket=self.ticket, session=self.session)
        self.resumed = Task.objects.create(ticket=self.ticket, session=self.session, parent_task=self.parked)

    def test_no_task_opens_an_empty_conversation(self) -> None:
        harness = resolve_harness()
        assert isinstance(harness, PydanticAiHarness)
        assert harness._history is None
        assert harness.resume_source is None

    def test_task_with_no_parked_ancestor_opens_an_empty_conversation(self) -> None:
        harness = resolve_harness(self.resumed)
        assert isinstance(harness, PydanticAiHarness)
        assert harness._history is None
        assert harness.resume_source is None

    def test_parked_ancestor_thread_is_rehydrated_and_consumed(self) -> None:
        from teatree.agents.pydantic_ai_resume import persist_parked_thread  # noqa: PLC0415

        agent = Agent(TestModel(custom_output_text="hi"))
        result = asyncio.run(agent.run("hello"))
        persist_parked_thread(self.parked, result.all_messages())

        harness = resolve_harness(self.resumed)

        assert isinstance(harness, PydanticAiHarness)
        assert harness._history == result.all_messages()
        # (#2916) resume_source records the popped ancestor so a caller that
        # refuses the dispatch before a genuine open can restore the thread.
        assert harness.resume_source == self.parked
        # Single-use: a second resolve for the same chain finds nothing left.
        harness_again = resolve_harness(self.resumed)
        assert harness_again._history is None
        assert harness_again.resume_source is None

    def test_claude_sdk_backend_ignores_task_entirely(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "claude_sdk")
        assert isinstance(resolve_harness(self.resumed), ClaudeSdkHarness)


class TestDriveThroughInjectedHarness(TestCase):
    """``_drive_with_heartbeat`` drives a run through ANY injected ``Harness``.

    Proves the seam is provider-agnostic: a pure :class:`FakeHarness` (no SDK)
    opens the session and the driver collects the stream through it, and the
    built options are passed straight through to ``harness.open``.
    """

    def setUp(self) -> None:
        self.ticket = planned_ticket()
        self.session = Session.objects.create(ticket=self.ticket)
        self.task = Task.objects.create(ticket=self.ticket, session=self.session)
        # A threaded ORM read under TestCase's wrapping SQLite transaction is a
        # harness artifact (the pre-run usage sample runs in a worker thread) —
        # stub it, as the ``fake_sdk`` scaffold does, so it is not production behaviour.
        self.task.renew_lease = lambda **_kw: None

    def test_driver_opens_the_injected_harness_and_collects(self) -> None:
        options = _build_options(self.task, "ctx", phase="coding", skills=[])
        harness = FakeHarness([assistant_text("hi"), result_message(session_id="s1")])
        watchdog = LoopWatchdog(max_runtime_seconds=0, max_turns=0, max_cost_usd=0.0)

        with patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))):
            outcome = asyncio.run(_drive_with_heartbeat(self.task, "p", options, harness, watchdog=watchdog))

        assert harness.opened_options is options
        assert outcome.stuck_reason is None
        assert outcome.agent_text == "hi"
        assert outcome.result_message is not None
        assert outcome.result_message.session_id == "s1"

    def test_driver_drives_a_real_pydantic_ai_harness_end_to_end(self) -> None:
        # A REAL PydanticAiHarness (real pydantic_ai Agent + TestModel, no
        # network) driven through the harness-agnostic driver — proves the
        # translated AssistantMessage/ResultMessage vocabulary round-trips
        # through the SAME `_collect` the ClaudeSdkHarness uses.
        options = _build_options(self.task, "ctx", phase="coding", skills=[])
        harness = PydanticAiHarness(model=TestModel(custom_output_text="hello from pydantic_ai"))
        watchdog = LoopWatchdog(max_runtime_seconds=0, max_turns=0, max_cost_usd=0.0)

        with patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))):
            outcome = asyncio.run(_drive_with_heartbeat(self.task, "p", options, harness, watchdog=watchdog))

        assert outcome.stuck_reason is None
        assert outcome.agent_text == "hello from pydantic_ai"
        assert outcome.result_message is not None
        assert outcome.result_message.is_error is False


class TestPydanticAiThread:
    """``pydantic_ai_thread`` extracts a session's live history, else ``None`` (#2886)."""

    def test_pydantic_ai_session_yields_its_accumulated_history(self) -> None:
        agent = Agent(TestModel(custom_output_text="hi"))
        session = PydanticAiHarnessSession(agent, model_name="test")

        assert pydantic_ai_thread(session) == session.history

    def test_non_pydantic_ai_session_yields_none(self) -> None:
        assert pydantic_ai_thread(FakeHarnessSession([])) is None


class TestRunHeadlessDrivesPydanticAiHarness(TestCase):
    """``run_agent`` genuinely dispatches through ``PydanticAiHarness`` when selected."""

    def setUp(self) -> None:
        self.ticket = planned_ticket()
        self.session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        self.task = Task.objects.create(ticket=self.ticket, session=self.session, phase="coding")
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")

    def test_pydantic_ai_harness_completes_a_real_run(self) -> None:
        # No `claude` binary check, no Anthropic credential needed — the
        # pydantic_ai harness is injected directly with a TestModel double.
        # ``plan_text`` is the phase-evidence gate's required key for ``planning``
        # (#1282-6) — unrelated to the harness under test. The phase is dispatched
        # as ``planning`` rather than ``coding`` because the injected harness is
        # built with no phase, so it carries no tool layer and the double emits no
        # tool call. On an ACTING phase that is refused
        # (:mod:`teatree.agents.action_verification`) and rightly so; a toolless
        # double can only honestly stand in for a phase that need not act.
        result_json = '{"summary": "test summary", "plan_text": "the plan"}'
        fake_harness = PydanticAiHarness(model=TestModel(custom_output_text=result_json))
        with (
            patch.object(runner_mod, "resolve_harness", return_value=fake_harness),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            attempt = run_agent(self.task, phase="planning", overlay_skill_metadata={})

        self.task.refresh_from_db()
        assert attempt.exit_code == 0
        assert self.task.status == Task.Status.COMPLETED
        assert attempt.result["summary"] == "test summary"

    def test_missing_backend_router_credential_records_a_clean_failure(self) -> None:
        # No injected model, no OPENAI_COMPATIBLE_BASE_URL/OPENAI_COMPATIBLE_API_KEY in the
        # environment — the lazily-resolved CredentialError is caught and
        # recorded, never an uncaught exception.
        ConfigSetting.objects.set_value("openai_compatible_model", "vendor/some-model")
        with (
            patch.dict(os.environ, {}, clear=False),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            os.environ.pop("OPENAI_COMPATIBLE_BASE_URL", None)
            os.environ.pop("OPENAI_COMPATIBLE_API_KEY", None)
            attempt = run_agent(self.task, phase="coding", overlay_skill_metadata={})

        self.task.refresh_from_db()
        assert attempt.exit_code == 1
        assert "openai_compatible_base_url" in attempt.error
        assert self.task.status == Task.Status.FAILED
        # Refused before any attempt work beyond the failure record.
        assert TaskAttempt.objects.filter(task=self.task).count() == 1

    def test_missing_credential_on_resume_preserves_the_parked_thread(self) -> None:
        # (souliane/teatree#2916 review) `resolve_harness` pops the parked
        # ancestor's thread as a side effect of BUILDING the harness — before
        # `harness.open()` ever runs, the only point the OpenAI-compatible backend's credential
        # resolves. A credential failure must restore what it just consumed,
        # or the conversation is lost even though the run never happened.
        from teatree.agents.pydantic_ai_resume import persist_parked_thread  # noqa: PLC0415

        agent = Agent(TestModel(custom_output_text="hi"))
        history = asyncio.run(agent.run("hello")).all_messages()
        parked = Task.objects.create(ticket=self.ticket, session=self.session)
        persist_parked_thread(parked, history)
        resumed_task = Task.objects.create(ticket=self.ticket, session=self.session, phase="coding", parent_task=parked)
        ConfigSetting.objects.set_value("openai_compatible_model", "vendor/some-model")

        with (
            patch.dict(os.environ, {}, clear=False),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            os.environ.pop("OPENAI_COMPATIBLE_BASE_URL", None)
            os.environ.pop("OPENAI_COMPATIBLE_API_KEY", None)
            attempt = run_agent(resumed_task, phase="coding", overlay_skill_metadata={})

        resumed_task.refresh_from_db()
        assert attempt.exit_code == 1
        assert "openai_compatible_base_url" in attempt.error
        assert resumed_task.status == Task.Status.FAILED
        self.ticket.refresh_from_db()
        assert str(parked.pk) in self.ticket.extra.get("pydantic_ai_threads", {})

    def test_non_credential_open_failure_on_resume_preserves_the_parked_thread(self) -> None:
        # AH-3 / #2916: `resolve_harness` pops the parked ancestor's thread when it
        # BUILDS the harness, before `open()` runs. If `open()` fails with anything
        # OTHER than CredentialError (a provider/transport/policy error), the popped
        # thread must STILL be restored — the run never opened, so it never consumed
        # it. Before the fix only CredentialError restored, so a plain failure lost
        # the conversation for good.
        agent = Agent(TestModel(custom_output_text="hi"))
        history = asyncio.run(agent.run("hello")).all_messages()
        parked = Task.objects.create(ticket=self.ticket, session=self.session)
        persist_parked_thread(parked, history)
        resumed_task = Task.objects.create(ticket=self.ticket, session=self.session, phase="coding", parent_task=parked)

        def _boom(_self: PydanticAiHarness, _options: object) -> object:
            msg = "backend router transport unavailable"
            raise RuntimeError(msg)

        with (
            patch.object(harness_mod.PydanticAiHarness, "_resolve_model", _boom),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
            pytest.raises(RuntimeError, match="backend router transport unavailable"),
        ):
            run_agent(resumed_task, phase="coding", overlay_skill_metadata={})

        # The non-CredentialError failure still propagates (the caller records it), but
        # the parked ancestor thread was restored, so the resume is recoverable.
        self.ticket.refresh_from_db()
        assert str(parked.pk) in self.ticket.extra.get("pydantic_ai_threads", {})


def _raising_stream(exc: Exception) -> object:
    """A ``FunctionModel`` stream function that raises *exc* on the model request.

    The trailing ``yield`` is unreachable but required so pydantic_ai treats the
    coroutine as an async generator (a streamed FunctionModel).
    """

    async def stream_fn(_messages: object, _info: AgentInfo) -> AsyncIterator[str]:
        await asyncio.sleep(0)
        raise exc
        yield ""

    return stream_fn


async def _tool_then_text_stream(messages: object, _info: AgentInfo) -> AsyncIterator[object]:
    """Request 1 issues a ``ping`` tool call; request 2 (after the return) yields text.

    Two model requests, so ``RunUsage.requests == 2`` — the fixture the num_turns
    and request-cap tests drive against.
    """
    await asyncio.sleep(0)
    returned = any(
        isinstance(part, ToolReturnPart)
        for message in (messages if isinstance(messages, list) else [])
        if isinstance(message, ModelRequest)
        for part in message.parts
    )
    if returned:
        yield "final answer"
    else:
        yield {0: DeltaToolCall(name="ping", json_args="{}", tool_call_id="c1")}


def _ping_toolset() -> FunctionToolset[None]:
    toolset: FunctionToolset[None] = FunctionToolset()
    toolset.add_function(lambda: "pong", name="ping")
    return toolset


def test_pydantic_ai_session_stamps_a_stable_nonempty_session_id() -> None:
    # A minted session_id is stamped on EVERY terminal ResultMessage and is stable
    # across turns (RED: the hardcoded "" gave the attempt no agent_session_id).
    session = PydanticAiHarnessSession(Agent(TestModel(custom_output_text="hi")), model_name="m")

    async def drive() -> list[ResultMessage]:
        results: list[ResultMessage] = []
        for _ in range(2):
            await session.query("go")
            results.extend([m async for m in session.receive_response() if isinstance(m, ResultMessage)])
        return results

    results = asyncio.run(drive())
    assert len(results) == 2
    assert results[0].session_id
    assert results[0].session_id == results[1].session_id == session.session_id


def test_pydantic_ai_sessions_mint_distinct_session_ids() -> None:
    first = PydanticAiHarnessSession(Agent(TestModel()), model_name="m")
    second = PydanticAiHarnessSession(Agent(TestModel()), model_name="m")
    assert first.session_id
    assert second.session_id
    assert first.session_id != second.session_id


def test_pydantic_ai_num_turns_reflects_the_request_count() -> None:
    # A tool-call turn followed by a text turn is TWO model requests, so the
    # terminal ResultMessage reports num_turns == 2 (RED: the hardcoded 1).
    agent: Agent[None, str] = Agent(FunctionModel(stream_function=_tool_then_text_stream), toolsets=[_ping_toolset()])
    session = PydanticAiHarnessSession(agent, model_name="m")

    async def drive() -> list[object]:
        await session.query("hi")
        return [message async for message in session.receive_response()]

    result = next(m for m in asyncio.run(drive()) if isinstance(m, ResultMessage))
    assert result.num_turns == 2


def _denial_gauntlet_stream(turns: int) -> Callable[[object, AgentInfo], AsyncIterator[object]]:
    """A streamed model that repeats a denylisted ``Bash`` call *turns* times, then yields text.

    Progress is derived from how many ``RetryPromptPart``s the history already
    carries (mirrors :func:`_tool_then_text_stream`'s returned-part probe) rather
    than closed-over mutable state, so the double stays stateless and safe to
    reuse across `asyncio.run` calls.
    """

    async def stream_fn(messages: object, _info: AgentInfo) -> AsyncIterator[object]:
        await asyncio.sleep(0)
        denials = sum(
            1
            for message in (messages if isinstance(messages, list) else [])
            if isinstance(message, ModelRequest)
            for part in message.parts
            if type(part).__name__ == "RetryPromptPart"
        )
        if denials < turns:
            args = json.dumps({"command": "rm -rf /"})
            yield {0: DeltaToolCall(name="Bash", json_args=args, tool_call_id=f"c{denials}")}
        else:
            yield "survived"

    return stream_fn


class TestPydanticAiHarnessShellRetryBudget:
    """A shell-exploration phase tolerates more corrective denials than the tight default.

    Before this, pydantic-ai's own per-tool retry ceiling (unset at Agent
    construction, defaulting to 1) and ``HardDenyToolset``'s cumulative denial cap
    (``gating.DEFAULT_MAX_DENIALS``, 3) both aborted the WHOLE dispatch on the very
    next corrective ``Bash`` retry — the crash behind ~28 of ``architectural_review``'s
    ~29 scheduled dispatches. ``PydanticAiHarness.open`` now widens both via
    ``LaneBToolConfig`` for the shell-exploration phase family, proved here through
    the real session surface (``query``/``receive_response``) the production driver uses.
    """

    @staticmethod
    async def _drive(phase: str, turns: int) -> ResultMessage:
        harness = PydanticAiHarness(model=FunctionModel(stream_function=_denial_gauntlet_stream(turns)), phase=phase)
        async with harness.open(ClaudeAgentOptions()) as session:
            await session.query("go")
            messages = [message async for message in session.receive_response()]
        return next(m for m in messages if isinstance(m, ResultMessage))

    def test_default_phase_aborts_on_three_denials(self) -> None:
        result = asyncio.run(self._drive("coding", turns=3))
        assert result.is_error is True

    def test_exploration_phase_survives_the_same_three_denials(self) -> None:
        result = asyncio.run(self._drive("architectural_review", turns=3))
        assert result.is_error is False


def test_hit_max_tokens_reads_the_final_response_finish_reason() -> None:
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart  # noqa: PLC0415 — test-local

    from teatree.agents.pydantic_ai_session import _hit_max_tokens  # noqa: PLC0415 — test-local

    truncated = ModelResponse(parts=[TextPart(content="partial")], finish_reason="length")
    clean = ModelResponse(parts=[TextPart(content="done")], finish_reason="stop")
    request = ModelRequest(parts=[UserPromptPart(content="hi")])
    assert _hit_max_tokens([request, truncated]) is True
    assert _hit_max_tokens([request, clean]) is False
    # The final ModelResponse wins even when an earlier one was truncated.
    assert _hit_max_tokens([truncated, request, clean]) is False
    # No ModelResponse at all → not a truncation.
    assert _hit_max_tokens([request]) is False
    assert _hit_max_tokens([]) is False


async def _truncated_text_stream(_messages: object, _info: AgentInfo) -> AsyncIterator[str]:
    """A single text delta standing in for a model cut off mid-envelope."""
    await asyncio.sleep(0)
    yield "partial truncated envelope"


class _LengthFinishModel(FunctionModel):
    """A ``FunctionModel`` whose streamed response reports a max-tokens (``'length'``) stop."""

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        async with super().request_stream(messages, model_settings, model_request_parameters, run_context) as stream:
            stream.finish_reason = "length"
            yield stream


def test_pydantic_ai_session_maps_a_length_finish_to_error_max_tokens() -> None:
    # A run that otherwise completes but whose final ModelResponse stopped on the
    # max_tokens ceiling (finish_reason='length') is surfaced as an is_error
    # ResultMessage(subtype="error_max_tokens"), never a success carrying the amputated
    # JSON envelope (RED: before the check the session yielded subtype="success").
    from teatree.agents.runner_failure_taxonomy import limit_match  # noqa: PLC0415 — test-local assertion

    agent: Agent[None, str] = Agent(_LengthFinishModel(stream_function=_truncated_text_stream))
    session = PydanticAiHarnessSession(agent, model_name="m")

    async def drive() -> list[object]:
        await session.query("hi")
        return [message async for message in session.receive_response()]

    messages = asyncio.run(drive())
    results = [m for m in messages if isinstance(m, ResultMessage)]
    assert len(results) == 1
    assert results[0].is_error is True
    assert results[0].subtype == "error_max_tokens"
    # A genuine FAILED, not a park — asserted on the decision point, not the matcher.
    assert limit_match(results[0]) is None


def test_pydantic_ai_session_maps_the_request_cap_to_error_max_turns() -> None:
    # A real per-run request cap (request_limit=1) against a model that wants a
    # 2nd request raises UsageLimitExceeded, which the session reports as an
    # is_error ResultMessage(subtype="error_max_turns") — a genuine FAILED the
    # taxonomy must never claim as a provider window (so it never parks). Asserted
    # on `limit_match` (the decision point) rather than `classify_limit` (a raw
    # substring matcher), so a vendor editing its own prose cannot void the test.
    from teatree.agents.runner_failure_taxonomy import limit_match  # noqa: PLC0415 — test-local assertion

    agent: Agent[None, str] = Agent(FunctionModel(stream_function=_tool_then_text_stream), toolsets=[_ping_toolset()])
    session = PydanticAiHarnessSession(agent, model_name="m", request_limit=1)

    async def drive() -> list[object]:
        await session.query("hi")
        return [message async for message in session.receive_response()]

    result = next(m for m in asyncio.run(drive()) if isinstance(m, ResultMessage))
    assert result.is_error is True
    assert result.subtype == "error_max_turns"
    assert limit_match(result) is None


class TestRunHeadlessPydanticAiFailureReporting(TestCase):
    """A pydantic_ai provider/run error is REPORTED through the driver, never a raw crash.

    The seam maps the error into the same ``is_error`` ``ResultMessage`` the
    claude_sdk lane yields, so the driver's failure taxonomy (park/rotate or a
    recorded FAILED) fires without any transport special-casing. Before the fix a
    429 propagated raw out of ``asyncio.run`` and ``run_agent`` re-raised it (a
    ``sdk_error`` FAILED-with-traceback), leaving the park path unreachable.
    """

    def setUp(self) -> None:
        self.ticket = planned_ticket()
        self.session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        self.task = Task.objects.create(ticket=self.ticket, session=self.session, phase="coding")
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")

    def _run_raising(self, exc: Exception) -> TaskAttempt:
        harness = PydanticAiHarness(model=FunctionModel(stream_function=_raising_stream(exc)))
        with (
            patch.object(runner_mod, "resolve_harness", return_value=harness),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            attempt = run_agent(self.task, phase="coding", overlay_skill_metadata={})
        self.task.refresh_from_db()
        return attempt

    def test_rate_limit_error_parks_when_autorecovery_on(self) -> None:
        ConfigSetting.objects.set_value("limit_autorecovery_enabled", value=True)
        exc = ModelHTTPError(status_code=429, model_name="m", body={"error": {"type": "rate_limit_error"}})
        attempt = self._run_raising(exc)

        assert self.task.status == Task.Status.PENDING, "PARKED for auto-resume, not a raw crash"
        assert "limit_parked: " in attempt.error
        assert "rate_limit" in attempt.error
        assert "Traceback" not in attempt.error
        window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.METERED)
        assert window is not None
        assert window.cause == "rate_limit"

    def test_rate_limit_error_fails_cleanly_when_autorecovery_off(self) -> None:
        ConfigSetting.objects.set_value("limit_autorecovery_enabled", value=False)
        exc = ModelHTTPError(status_code=429, model_name="m", body={"error": {"type": "rate_limit_error"}})
        attempt = self._run_raising(exc)

        assert self.task.status == Task.Status.FAILED
        assert attempt.error.startswith("rate_limit: ")
        assert "Traceback" not in attempt.error, "a classified limit failure, never a raw traceback"

    def test_overloaded_error_529_is_a_rate_limit(self) -> None:
        ConfigSetting.objects.set_value("limit_autorecovery_enabled", value=True)
        exc = ModelHTTPError(status_code=529, model_name="m", body={"error": {"type": "overloaded_error"}})
        attempt = self._run_raising(exc)

        assert self.task.status == Task.Status.PENDING
        assert "rate_limit" in attempt.error
        window = UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.METERED)
        assert window is not None
        assert window.cause == "rate_limit"

    def test_credit_body_400_is_api_credit_and_never_parks(self) -> None:
        # API-credit exhaustion has no timed window, so even with auto-recovery ON
        # it FAILS loud (add credits) and never parks.
        ConfigSetting.objects.set_value("limit_autorecovery_enabled", value=True)
        exc = ModelHTTPError(
            status_code=400,
            model_name="m",
            body={"error": {"type": "invalid_request_error", "message": "credit balance is too low"}},
        )
        attempt = self._run_raising(exc)

        assert self.task.status == Task.Status.FAILED
        assert attempt.error.startswith("api_credit: ")
        assert "console.anthropic.com" in attempt.error
        assert "subscription" not in attempt.error.casefold()
        assert UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.METERED) is None

    def test_usage_limit_exceeded_fails_error_max_turns_and_never_parks(self) -> None:
        # The run hit its own per-run request cap — a genuine FAILED (error_max_turns),
        # never a park, never a raw traceback.
        ConfigSetting.objects.set_value("limit_autorecovery_enabled", value=True)
        exc = UsageLimitExceeded("The next request would exceed the request_limit of 1")
        attempt = self._run_raising(exc)

        assert self.task.status == Task.Status.FAILED
        assert "error_max_turns" in attempt.error
        assert "Traceback" not in attempt.error
        assert UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.METERED) is None


class TestRunHeadlessCachedResumeParity(TestCase):
    """End-to-end park -> resume through the REAL ``resolve_harness`` (#2886).

    Unlike ``TestRunHeadlessDrivesPydanticAiHarness`` (which injects a fixed
    harness, bypassing resolution), this drives ``run_agent`` through the
    genuine ``resolve_harness(task)`` seam for BOTH the parking dispatch and
    the resumed continuation — proving the persisted thread actually reaches
    the resumed session's first turn, not just that the plumbing types check.
    """

    def setUp(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        self.ticket = planned_ticket()
        self.session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        self.task = Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            phase="coding",
        )

    def test_resumed_dispatch_rehydrates_the_parked_conversation(self) -> None:
        park_json = json.dumps({"summary": "blocked", "needs_user_input": True, "user_input_reason": "need it"})
        finish_json = json.dumps({"summary": "done", "files_modified": ["a.py"]})
        responses = [park_json, finish_json]
        captured_message_counts: list[int] = []

        async def stream_fn(messages: object, _info: AgentInfo) -> AsyncIterator[str]:
            await asyncio.sleep(0)
            captured_message_counts.append(len(messages))
            yield responses[len(captured_message_counts) - 1]

        with (
            patch.object(
                harness_mod.PydanticAiHarness,
                "_resolve_model",
                lambda self, options: FunctionModel(stream_function=stream_fn),
            ),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            park_attempt = run_agent(self.task, phase="coding", overlay_skill_metadata={})

        self.task.refresh_from_db()
        assert park_attempt.result["needs_user_input"] is True
        self.ticket.refresh_from_db()
        assert str(self.task.pk) in self.ticket.extra.get("pydantic_ai_threads", {})

        from teatree.core.models.task_handoff import schedule_resume  # noqa: PLC0415 — deferred: Django-dependent

        resumed_task = schedule_resume(self.task, answer="go ahead")

        with (
            patch.object(
                harness_mod.PydanticAiHarness,
                "_resolve_model",
                lambda self, options: FunctionModel(stream_function=stream_fn),
            ),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            resume_attempt = run_agent(resumed_task, phase="coding", overlay_skill_metadata={})

        assert resume_attempt.result["summary"] == "done"
        # The resumed turn's model call carried more messages than a bare
        # first prompt would — the rehydrated park thread landed on it.
        assert captured_message_counts[1] > 1
        self.ticket.refresh_from_db()
        assert str(self.task.pk) not in self.ticket.extra.get("pydantic_ai_threads", {})


class TestPydanticAiHarnessRegulatedPathGate(TestCase):
    """#2887: on a regulated lane, a model off the allowlist never reaches the provider."""

    def setUp(self) -> None:
        os.environ.pop("OPENAI_COMPATIBLE_BASE_URL", None)
        os.environ.pop("OPENAI_COMPATIBLE_API_KEY", None)

    def test_model_off_the_allowlist_raises_before_credential_resolution(self) -> None:
        # No backend credential configured — proves the regulated-path allowlist check
        # fires FIRST (a config-policy ValueError), not the credential/endpoint check.
        ConfigSetting.objects.set_value("enforce_regulated_path", value=True)
        ConfigSetting.objects.set_value("regulated_path_model_allowlist", value=["anthropic/"])
        options = ClaudeAgentOptions(model="deepseek/deepseek-v4-pro")

        with pytest.raises(ValueError, match="not eligible for the regulated path"):
            PydanticAiHarness()._resolve_model(options)

    def test_unenforced_lane_reaches_the_credential_step(self) -> None:
        # Default enforce_regulated_path=False — the factory lane is unrestricted,
        # so resolution proceeds to the (here unconfigured) credential step.
        options = ClaudeAgentOptions(model="deepseek/deepseek-v4-pro")

        with pytest.raises(CredentialError, match="openai_compatible_base_url"):
            PydanticAiHarness()._resolve_model(options)

    def test_allowlisted_model_reaches_the_credential_step(self) -> None:
        ConfigSetting.objects.set_value("enforce_regulated_path", value=True)
        ConfigSetting.objects.set_value("regulated_path_model_allowlist", value=["deepseek/"])
        options = ClaudeAgentOptions(model="deepseek/deepseek-v4-pro")

        with pytest.raises(CredentialError, match="openai_compatible_base_url"):
            PydanticAiHarness()._resolve_model(options)


class TestPydanticAiHarnessSession:
    """The ``pydantic_ai`` session adapter — query/receive_response/interrupt."""

    def test_round_trip_yields_the_claude_sdk_message_vocabulary(self) -> None:
        agent = Agent(TestModel(custom_output_text="hi there"))
        session = PydanticAiHarnessSession(agent, model_name="test")

        async def drive() -> list[object]:
            await session.query("hello")
            return [m async for m in session.receive_response()]

        messages = asyncio.run(drive())

        assert len(messages) == 2
        assistant, result = messages
        assert isinstance(assistant, AssistantMessage)
        assert assistant.content == [TextBlock(text="hi there")]
        assert isinstance(result, ResultMessage)
        assert result.is_error is False
        assert result.result == "hi there"

    def test_no_pending_query_yields_nothing(self) -> None:
        agent = Agent(TestModel(custom_output_text="unused"))
        session = PydanticAiHarnessSession(agent, model_name="test")

        async def drive() -> list[object]:
            return [m async for m in session.receive_response()]

        assert asyncio.run(drive()) == []

    def test_multi_turn_keeps_message_history_across_calls(self) -> None:
        agent = Agent(TestModel(custom_output_text="ack"))
        session = PydanticAiHarnessSession(agent, model_name="test")

        async def drive() -> None:
            await session.query("first")
            _ = [m async for m in session.receive_response()]
            await session.query("second")
            _ = [m async for m in session.receive_response()]

        asyncio.run(drive())
        # Two full request/response exchanges recorded in history.
        assert len(session._history) == 4

    def test_seeded_history_is_sent_on_the_first_turn(self) -> None:
        # (#2886) A resumed session is constructed with a prior conversation —
        # the FIRST run_stream must already carry it, proving cached-resume
        # parity with ClaudeSDKClient's `--resume` continuation.
        captured: list[int] = []

        async def stream_fn(messages: object, _info: AgentInfo) -> AsyncIterator[str]:
            await asyncio.sleep(0)
            captured.append(len(messages))
            yield "ack"

        seed_agent = Agent(TestModel(custom_output_text="seed turn"))
        seed_result = asyncio.run(seed_agent.run("seed prompt"))
        seed_history = seed_result.all_messages()

        agent = Agent(FunctionModel(stream_function=stream_fn))
        session = PydanticAiHarnessSession(agent, model_name="test", history=seed_history)

        assert session.history == seed_history

        async def drive() -> None:
            await session.query("continue")
            _ = [m async for m in session.receive_response()]

        asyncio.run(drive())

        # The model saw the seeded turn's messages PLUS the new prompt.
        assert captured == [len(seed_history) + 1]
        assert len(session.history) > len(seed_history)

    def test_no_history_seed_starts_empty(self) -> None:
        agent = Agent(TestModel(custom_output_text="unused"))
        session = PydanticAiHarnessSession(agent, model_name="test")
        assert session.history == []

    def test_interrupt_before_any_query_is_a_safe_no_op(self) -> None:
        agent = Agent(TestModel())
        session = PydanticAiHarnessSession(agent, model_name="test")
        asyncio.run(session.interrupt())  # must not raise

    def test_interrupt_cancels_an_in_flight_response_and_yields_nothing(self) -> None:
        # Synchronize on a real chunk being emitted rather than a wall-clock
        # sleep — a fixed-delay race is flaky under CPU contention (the
        # producer's own sleeps can lag behind an unrelated sleep(N) in the
        # driving coroutine on a loaded machine). Waiting for `chunk_seen`
        # proves the drain task has genuinely started before `interrupt()`
        # fires, and 49 remaining 0.05s-spaced chunks leave ample margin for
        # the cancellation to land before the stream would finish naturally.
        chunk_seen = asyncio.Event()

        async def slow_stream(_messages: object, _info: AgentInfo) -> AsyncIterator[str]:
            for i in range(50):
                yield f"chunk{i} "
                chunk_seen.set()
                await asyncio.sleep(0.05)

        agent = Agent(FunctionModel(stream_function=slow_stream))
        session = PydanticAiHarnessSession(agent, model_name="test")

        async def drive() -> list[object]:
            await session.query("hello")
            consumer = asyncio.ensure_future(_collect_all(session))
            await chunk_seen.wait()
            await session.interrupt()
            return await consumer

        assert asyncio.run(drive()) == []

    def test_interrupt_stops_token_generation_not_just_the_local_consumer(self) -> None:
        # The property that matters is that the PROVIDER REQUEST unwinds — token
        # generation ceases and the connection closes — rather than the run
        # continuing to bill in the background while a local consumer walks away.
        # Asserted on the producer's own emission count, which is what "stopped"
        # means on any transport; a private handle on whichever object happens to
        # carry the in-flight stream is an implementation detail, not the contract.
        chunk_seen = asyncio.Event()
        produced: list[str] = []

        async def slow_stream(_messages: object, _info: AgentInfo) -> AsyncIterator[str]:
            for i in range(50):
                produced.append(f"chunk{i}")
                yield f"chunk{i} "
                chunk_seen.set()
                await asyncio.sleep(0.05)

        agent = Agent(FunctionModel(stream_function=slow_stream))
        session = PydanticAiHarnessSession(agent, model_name="test")

        async def drive() -> int:
            await session.query("hello")
            consumer = asyncio.ensure_future(_collect_all(session))
            await chunk_seen.wait()
            # Sampled BEFORE the interrupt and re-read after a wait, never after
            # awaiting the consumer: an interrupt that only set a flag would leave
            # the consumer blocked until the run finished naturally, and a delta
            # measured from THAT point is zero either way — an assertion that
            # cannot fail. Measured against a flag-only interrupt, this window
            # sees 5 further chunks.
            at_interrupt = len(produced)
            await session.interrupt()
            await asyncio.sleep(0.3)  # six further 0.05s chunk intervals
            emitted_after = len(produced) - at_interrupt
            consumer.cancel()
            with suppress(asyncio.CancelledError):
                await consumer
            return emitted_after

        assert asyncio.run(drive()) == 0, "the provider must stop generating, not keep billing in the background"

    def test_external_cancellation_propagates_instead_of_being_swallowed(self) -> None:
        # A timeout unrelated to interrupt() (e.g. headless._drive_with_heartbeat's
        # asyncio.wait_for runtime ceiling) must NOT be silently absorbed as if it
        # were a deliberate interrupt() — swallowing it would report an empty
        # result instead of surfacing the runtime-breach TimeoutError the
        # watchdog contract depends on.
        async def slow_stream(_messages: object, _info: AgentInfo) -> AsyncIterator[str]:
            for i in range(50):
                await asyncio.sleep(0.05)
                yield f"chunk{i} "

        agent = Agent(FunctionModel(stream_function=slow_stream))
        session = PydanticAiHarnessSession(agent, model_name="test")

        async def drive() -> list[object]:
            await session.query("hello")
            return await asyncio.wait_for(_collect_all(session), timeout=0.2)

        with pytest.raises(TimeoutError):
            asyncio.run(drive())

    def test_usage_and_model_usage_are_populated_from_the_stream(self) -> None:
        agent = Agent(TestModel(custom_output_text="hi there"))
        session = PydanticAiHarnessSession(agent, model_name="gpt-test")

        async def drive() -> list[object]:
            await session.query("hello")
            return [m async for m in session.receive_response()]

        _, result = asyncio.run(drive())

        assert isinstance(result, ResultMessage)
        assert result.usage is not None
        assert result.usage["input_tokens"] is not None
        assert result.model_usage == {"gpt-test": {}}


async def _collect_all(session: PydanticAiHarnessSession) -> list[object]:
    return [m async for m in session.receive_response()]


class TestResolveEffort:
    def test_is_a_public_seam(self) -> None:
        # The eval pydantic_ai runner reuses this effort-vocabulary guard as a
        # cross-module seam, so it must be a PUBLIC name — not a private
        # ``_resolve_effort`` reached through the underscore.
        assert hasattr(harness_mod, "resolve_effort")
        assert not hasattr(harness_mod, "_resolve_effort")

    def test_valid_shared_effort_passes_through(self) -> None:
        # AH-2: resolve_effort takes the NEUTRAL HarnessOptions, never the vendor type.
        assert resolve_effort(HarnessOptions(effort="xhigh")) == "xhigh"

    def test_claude_only_max_is_dropped(self) -> None:
        # "max" is on claude_sdk's EFFORT_SCALE but not pydantic_ai's
        # ReasoningEffort vocabulary — the harness must never forward it.
        assert resolve_effort(HarnessOptions(effort="max")) is None

    def test_absent_effort_is_none(self) -> None:
        assert resolve_effort(HarnessOptions(effort=None)) is None


class TestPydanticAiModelIdNormalization(TestCase):
    """``_resolve_model`` sends the endpoint an id its catalog carries."""

    @pytest.fixture(autouse=True)
    def _backend_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://api.example.invalid/v1")
        monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "dummy-backend-test-value")

    @staticmethod
    def _configured_harness() -> PydanticAiHarness:
        return PydanticAiHarness(
            config=PydanticAiModelConfig(backend=OpenAICompatibleLaneConfig(model="vendor/some-model"))
        )

    def test_claude_dash_form_default_is_normalised_to_the_configured_model(self) -> None:
        # The bug: options.model carries a teatree-abstract-tier default in Claude
        # dash-form (claude-opus-4-8), which the endpoint does NOT carry. It must be
        # normalised to the configured id, never sent verbatim.
        model = self._configured_harness()._resolve_model(HarnessOptions(model="claude-opus-4-8"))
        assert model.model_name == "vendor/some-model"

    def test_no_model_pin_resolves_to_the_configured_model(self) -> None:
        model = self._configured_harness()._resolve_model(HarnessOptions())
        assert model.model_name == "vendor/some-model"

    def test_explicit_provider_native_pin_passes_through(self) -> None:
        model = self._configured_harness()._resolve_model(HarnessOptions(model="deepseek/deepseek-v4-pro"))
        assert model.model_name == "deepseek/deepseek-v4-pro"

    def test_a_model_off_the_regulated_allowlist_is_refused(self) -> None:
        ConfigSetting.objects.set_value("enforce_regulated_path", value=True)
        ConfigSetting.objects.set_value("regulated_path_model_allowlist", value=["anthropic/"])
        with pytest.raises(ValueError, match="not eligible for the regulated path"):
            self._configured_harness()._resolve_model(HarnessOptions(model="deepseek/deepseek-v4-pro"))


class TestBuildOpenAICompatibleProvider(TestCase):
    """``build_openai_compatible_provider`` — the configured provider + the x-lane header."""

    @pytest.fixture(autouse=True)
    def _backend_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://api.example.invalid/v1")
        monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "dummy-backend-test-value")

    def test_factory_lane_rides_the_x_lane_header(self) -> None:
        provider = build_openai_compatible_provider(OpenAICompatibleLaneConfig(lane=LANE_FACTORY))
        assert provider.client.default_headers["x-lane"] == "factory"
        assert str(provider.client.base_url).rstrip("/") == "https://api.example.invalid/v1"

    def test_eval_lane_rides_the_x_lane_header(self) -> None:
        provider = build_openai_compatible_provider(OpenAICompatibleLaneConfig(lane=LANE_EVAL))
        assert provider.client.default_headers["x-lane"] == "eval"

    def test_bulk_lane_rides_the_x_lane_header(self) -> None:
        # A secondary overlay's cheap bulk-leg lane: a router DSL rule keys on
        # ``headers["x-lane"] == "bulk"``.
        provider = build_openai_compatible_provider(OpenAICompatibleLaneConfig(lane=LANE_BULK))
        assert provider.client.default_headers["x-lane"] == "bulk"

    def _capture_pass_path(self, pass_path: str | None) -> str:
        captured: dict[str, str] = {}

        def _spy(*, base_url: str, model: str, credential: object) -> object:
            captured["path"] = credential._effective_spec().pass_path
            return OpenAICompatibleBackend(
                api_key="sk", base_url="https://api.example.invalid/v1", model="vendor/some-model"
            )

        with patch.object(pyconfig_mod, "resolve_openai_compatible_backend", _spy):
            build_openai_compatible_provider(OpenAICompatibleLaneConfig(lane=LANE_FACTORY, credential_entry=pass_path))
        return captured["path"]

    def test_configured_pass_path_is_injected_into_the_credential(self) -> None:
        # The openai_compatible_credential_entry DB-home setting points teatree at an existing
        # per-account pass entry with NO copy (plan §3.6 / task item 4).
        path = "vendor/office@example.com/api-key"
        assert self._capture_pass_path(path) == path

    def test_empty_pass_path_has_no_builtin(self) -> None:
        # No built-in default: with no configured openai_compatible_credential_entry the credential's
        # effective pass_path stays None — it resolves from OPENAI_COMPATIBLE_API_KEY or fails loud.
        assert self._capture_pass_path(None) is None


class TestPydanticAiStepCap(TestCase):
    """The per-run sequential-request cap via pydantic_ai ``UsageLimits`` (plan §4 guardrail #1)."""

    def test_positive_limit_becomes_usage_limits(self) -> None:
        session = PydanticAiHarnessSession(Agent(TestModel()), model_name="t", request_limit=5)
        limits = session._usage_limits()
        assert limits is not None
        assert limits.request_limit == 5

    def test_disabled_limit_is_uncapped(self) -> None:
        for value in (0, None):
            with self.subTest(value=value):
                session = PydanticAiHarnessSession(Agent(TestModel()), model_name="t", request_limit=value)
                assert session._usage_limits() is None

    def test_resolve_harness_reads_the_configured_request_limit_synchronously(self) -> None:
        # Resolved SYNC in resolve_harness (before asyncio.run) — a read inside the
        # async open would fail safe to the default.
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        ConfigSetting.objects.set_value("pydantic_ai_request_limit", value=3)
        harness = resolve_harness(phase="coding")
        assert isinstance(harness, PydanticAiHarness)
        assert harness._backend.request_limit == 3

    def test_open_threads_the_request_limit_into_the_session(self) -> None:
        harness = PydanticAiHarness(
            model=TestModel(), config=PydanticAiModelConfig(backend=OpenAICompatibleLaneConfig(request_limit=4))
        )

        async def drive() -> int | None:
            async with harness.open(ClaudeAgentOptions()) as session:
                assert isinstance(session, PydanticAiHarnessSession)
                return session._request_limit

        assert asyncio.run(drive()) == 4

    def test_positive_max_turns_wins_over_request_limit(self) -> None:
        harness = PydanticAiHarness(
            model=TestModel(), config=PydanticAiModelConfig(backend=OpenAICompatibleLaneConfig(request_limit=4))
        )

        async def drive() -> int | None:
            async with harness.open(ClaudeAgentOptions(max_turns=3)) as session:
                assert isinstance(session, PydanticAiHarnessSession)
                return session._request_limit

        assert asyncio.run(drive()) == 3

    def test_zero_max_turns_keeps_the_lane_request_limit(self) -> None:
        # Headless dispatch sends max_turns=0 → the lane's request_limit is untouched, so an
        # uncapped dispatch stays byte-identical and only a positive caller cap changes behaviour.
        harness = PydanticAiHarness(
            model=TestModel(), config=PydanticAiModelConfig(backend=OpenAICompatibleLaneConfig(request_limit=4))
        )

        async def drive() -> int | None:
            async with harness.open(ClaudeAgentOptions(max_turns=0)) as session:
                assert isinstance(session, PydanticAiHarnessSession)
                return session._request_limit

        assert asyncio.run(drive()) == 4

    def test_zero_max_turns_and_no_request_limit_stays_uncapped(self) -> None:
        harness = PydanticAiHarness(model=TestModel())

        async def drive() -> bool:
            async with harness.open(ClaudeAgentOptions(max_turns=0)) as session:
                assert isinstance(session, PydanticAiHarnessSession)
                return session._usage_limits() is None

        assert asyncio.run(drive()) is True

    def test_default_setting_is_a_real_turn_budget(self) -> None:
        # A live Lane-B task runs ~16 model requests, so the cap is a generous budget
        # well above that reality (the old 5 refused mid-task before ``open()``).
        assert get_effective_settings().pydantic_ai_request_limit == 40


class TestPydanticAiMaxTokens(TestCase):
    """The per-request ``max_tokens`` ceiling reaches the model settings (binding-agnostic)."""

    def test_default_setting_is_the_owner_chosen_ceiling(self) -> None:
        from teatree.config.settings import PYDANTIC_AI_MAX_TOKENS_DEFAULT  # noqa: PLC0415 — test-local

        # The owner chose 64000 (a generous ceiling paired with a truncation alert), carried
        # in a named constant, not a magic literal on the field.
        assert PYDANTIC_AI_MAX_TOKENS_DEFAULT == 64000
        assert get_effective_settings().pydantic_ai_max_tokens == PYDANTIC_AI_MAX_TOKENS_DEFAULT

    def test_default_ceiling_fits_every_tier_models_own_output_limit(self) -> None:
        """The one global ceiling is accepted by the SMALLEST tier model, not just the frontier one.

        ``build_model_settings`` merges this single value into every request whatever tier the
        dispatched phase resolved to, and the Anthropic Messages API rejects a ``max_tokens``
        above the addressed model's own limit with a 400 rather than clamping it. A ceiling
        chosen against the frontier model's 128K would therefore 400 every ``cheap``-tier
        dispatch (Haiku 4.5 caps at 64K) while looking correct on ``frontier``.
        """
        from teatree.agents.model_tiering import TIER_MODELS  # noqa: PLC0415 — test-local
        from teatree.config.settings import PYDANTIC_AI_MAX_TOKENS_DEFAULT  # noqa: PLC0415 — test-local

        # Published per-model output ceilings for the ids TIER_MODELS resolves to.
        max_output_tokens = {
            "claude-opus-5": 128_000,
            "claude-sonnet-5": 128_000,
            "claude-haiku-4-5": 64_000,
        }
        assert set(TIER_MODELS.values()) <= max_output_tokens.keys(), (
            "TIER_MODELS gained a model with no known output limit — confirm its ceiling before "
            "trusting PYDANTIC_AI_MAX_TOKENS_DEFAULT against it"
        )
        smallest = min(max_output_tokens[model] for model in TIER_MODELS.values())
        assert smallest >= PYDANTIC_AI_MAX_TOKENS_DEFAULT

    def test_resolve_harness_reads_the_configured_max_tokens_synchronously(self) -> None:
        # Resolved SYNC in resolve_harness (before asyncio.run) — a read inside the
        # async open would fail safe to the default.
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        ConfigSetting.objects.set_value("pydantic_ai_max_tokens", value=12000)
        harness = resolve_harness(phase="coding")
        assert isinstance(harness, PydanticAiHarness)
        assert harness._max_tokens == 12000

    def test_open_threads_max_tokens_into_the_agent_model_settings(self) -> None:
        harness = PydanticAiHarness(model=TestModel(), config=PydanticAiModelConfig(max_tokens=9000))

        async def drive() -> object:
            async with harness.open(ClaudeAgentOptions()) as session:
                assert isinstance(session, PydanticAiHarnessSession)
                return session._agent.model_settings

        assert asyncio.run(drive()) == {"max_tokens": 9000}


class TestVerifierPinnedToClaude(TestCase):
    """A verification phase stays on claude_sdk even when pydantic_ai is configured (plan §4 #2)."""

    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_AGENT_HARNESS", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def setUp(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")

    def test_verification_phase_forces_claude_sdk(self) -> None:
        for phase in ("reviewing", "requesting_review", "testing"):
            with self.subTest(phase=phase):
                assert isinstance(resolve_harness(phase=phase), ClaudeSdkHarness)

    def test_maker_phase_uses_the_configured_pydantic_ai(self) -> None:
        for phase in ("coding", "planning", "debugging"):
            with self.subTest(phase=phase):
                assert isinstance(resolve_harness(phase=phase), PydanticAiHarness)

    def test_no_phase_uses_the_configured_pydantic_ai(self) -> None:
        assert isinstance(resolve_harness(), PydanticAiHarness)


class TestResolveDispatchProvider(TestCase):
    """The Layer-2 provider follows the SAME phase pin Layer 1 does.

    ``resolve_harness`` validates the CONFIGURED pair and then lets ``PHASE_HARNESS`` flip
    Layer 1 for a verification phase. The credential must follow that flip, or a valid
    ``pydantic_ai`` deployment hands the claude_sdk child-env resolver a provider invalid
    under the harness it is actually running and every verification dispatch is refused.
    """

    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_AGENT_HARNESS", raising=False)
        monkeypatch.delenv("T3_AGENT_HARNESS_PROVIDER", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_no_configured_pin_stays_unpinned(self) -> None:
        assert resolve_dispatch_provider(phase="testing") is None

    def test_unpinned_phase_keeps_the_configured_provider(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        ConfigSetting.objects.set_value("agent_harness_provider", "anthropic_api")
        for phase in (None, "coding", "debugging", "shipping"):
            with self.subTest(phase=phase):
                assert resolve_dispatch_provider(phase=phase) is AgentHarnessProvider.ANTHROPIC_API

    def test_verification_pin_drops_a_provider_invalid_under_the_pinned_harness(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        ConfigSetting.objects.set_value("agent_harness_provider", "anthropic_api")
        for phase in ("reviewing", "requesting_review", "testing"):
            with self.subTest(phase=phase):
                assert resolve_dispatch_provider(phase=phase) is None

    def test_the_drop_is_warned_never_silent(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        ConfigSetting.objects.set_value("agent_harness_provider", "anthropic_api")
        with self.assertLogs("teatree.agents.harness", level="WARNING") as logs:
            resolve_dispatch_provider(phase="testing")
        assert any("anthropic_api" in message and "claude_sdk" in message for message in logs.output)

    def test_a_flip_onto_a_harness_the_pin_is_still_valid_under_keeps_it(self) -> None:
        # Not "drop the pin on ANY flip" — only on one the flip INVALIDATES. An
        # overlay-registered backend that also accepts ``anthropic_api`` keeps the pin,
        # so the drop can never over-suppress a credential that still applies.
        register_harness(
            "shares_anthropic_api",
            lambda context: ClaudeSdkHarness(),
            valid_providers=frozenset({AgentHarnessProvider.ANTHROPIC_API.value}),
        )
        self.addCleanup(harness_registry._REGISTRY.pop, "shares_anthropic_api", None)
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        ConfigSetting.objects.set_value("agent_harness_provider", "anthropic_api")
        with patch.object(harness_mod, "resolve_phase_harness", return_value="shares_anthropic_api"):
            assert resolve_dispatch_provider(phase="testing") is AgentHarnessProvider.ANTHROPIC_API

    def test_an_invalid_pair_no_pin_explains_is_left_for_the_dispatch_guard(self) -> None:
        # The control: a pair the phase pin does NOT explain is passed through untouched, so
        # ``resolve_harness``'s InvalidHarnessProviderError still fires on it.
        with patch.dict(
            os.environ,
            {"T3_AGENT_HARNESS": "claude_sdk", "T3_AGENT_HARNESS_PROVIDER": "openai_compatible"},
        ):
            assert resolve_dispatch_provider(phase="coding") is AgentHarnessProvider.OPENAI_COMPATIBLE
            with pytest.raises(InvalidHarnessProviderError):
                resolve_harness(phase="coding")


class TestOpenAICompatibleLaneAndModelCallSite(TestCase):
    """The call-site plumbing: config-driven endpoint + model + lane, resolved TOGETHER.

    ``resolve_harness`` resolves the generic DB-home backend settings SYNCHRONOUSLY into
    ``OpenAICompatibleLaneConfig``, and ``_resolve_model`` binds base_url + key + model +
    ``x-lane`` together for the selected lane — never a half-swap.
    """

    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_AGENT_HARNESS", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_OPENAI_COMPATIBLE_LANE", raising=False)
        monkeypatch.delenv("T3_OPENAI_COMPATIBLE_MODEL", raising=False)
        monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://api.example.invalid/v1")
        monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "dummy-backend-test-value")

    def test_configured_model_threads_into_the_resolved_model(self) -> None:
        # The whole-lane model selection: an overlay pointing at its own model id
        # resolves it, driven by config — never a hardcoded provider handle.
        harness = PydanticAiHarness(
            config=PydanticAiModelConfig(backend=OpenAICompatibleLaneConfig(model="vendor/other-model"))
        )
        model = harness._resolve_model(HarnessOptions(model="claude-opus-4-8"))
        assert model.model_name == "vendor/other-model"

    def test_an_unconfigured_model_fails_loud_rather_than_guessing(self) -> None:
        with pytest.raises(UnconfiguredOpenAICompatibleModelError, match="openai_compatible_model"):
            PydanticAiHarness()._resolve_model(HarnessOptions(model="claude-opus-4-8"))

    def test_resolve_harness_reads_the_generic_backend_settings_synchronously(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        ConfigSetting.objects.set_value("openai_compatible_lane", "bulk")
        ConfigSetting.objects.set_value("openai_compatible_model", "vendor/other-model")
        ConfigSetting.objects.set_value("openai_compatible_base_url", "https://db.example.invalid/v1")
        ConfigSetting.objects.set_value("openai_compatible_credential_entry", "vendor/account/api-key")
        harness = resolve_harness(phase="coding")
        assert isinstance(harness, PydanticAiHarness)
        assert harness._backend.lane == "bulk"
        assert harness._backend.model == "vendor/other-model"
        assert harness._backend.base_url == "https://db.example.invalid/v1"
        assert harness._backend.credential_entry == "vendor/account/api-key"

    def test_default_lane_is_factory_with_nothing_configured(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        harness = resolve_harness(phase="coding")
        assert isinstance(harness, PydanticAiHarness)
        assert harness._backend.lane == "factory"
        assert harness._backend.model is None
        assert harness._backend.credential_entry is None

    def test_base_url_key_model_and_x_lane_resolve_together_for_the_lane(self) -> None:
        # One call binds base_url + key + model + x-lane for the right lane — a whole
        # binding, not a half-swap.
        harness = PydanticAiHarness(
            config=PydanticAiModelConfig(backend=OpenAICompatibleLaneConfig(lane=LANE_BULK, model="vendor/other-model"))
        )
        model = harness._resolve_model(HarnessOptions(model="claude-opus-4-8"))
        assert model.model_name == "vendor/other-model"
        client = model.client
        assert str(client.base_url).rstrip("/") == "https://api.example.invalid/v1"
        assert client.api_key == "dummy-backend-test-value"
        assert client.default_headers["x-lane"] == "bulk"


class TestOpenAICompatibleInertByDefault(TestCase):
    """(b): default config → ZERO the OpenAI-compatible backend involvement. The whole feature ships DARK."""

    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_AGENT_HARNESS", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        # Nothing configured, and no agent_harness row: the default path
        # must never touch the OpenAI-compatible backend credential/base-url resolution.
        monkeypatch.delenv("OPENAI_COMPATIBLE_BASE_URL", raising=False)
        monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)

    def test_default_harness_is_claude_sdk_and_never_resolves_backend(self) -> None:
        for phase in (None, "coding", "planning", "reviewing"):
            with self.subTest(phase=phase):
                assert isinstance(resolve_harness(phase=phase), ClaudeSdkHarness)

    def test_default_settings_do_not_route_to_backend(self) -> None:
        settings = get_effective_settings()
        assert settings.agent_harness.value == "claude_sdk"
        assert settings.openai_compatible_lane == "factory"
        assert settings.openai_compatible_model == ""

    def test_building_the_default_harness_makes_no_backend_credential_call(self) -> None:
        # Selecting the default backend must not itself resolve an the OpenAI-compatible backend
        # credential/base-url — proves the DARK feature stays inert with no key set.
        with patch.object(pyconfig_mod, "resolve_openai_compatible_backend") as spy:
            harness = resolve_harness(phase="coding")
        assert isinstance(harness, ClaudeSdkHarness)
        spy.assert_not_called()


class TestOpenAICompatibleDispatchInertByDefault(TestCase):
    """DEFAULT config keeps every dispatch on claude_sdk — the backend is inert until enabled."""

    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_AGENT_HARNESS", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_default_harness_is_claude_sdk(self) -> None:
        assert get_effective_settings().agent_harness.value == "claude_sdk"

    def test_every_phase_stays_on_claude_sdk_by_default(self) -> None:
        for phase in ("coding", "reviewing", "testing", "planning", "requesting_review", "shipping"):
            with self.subTest(phase=phase):
                assert isinstance(resolve_harness(phase=phase), ClaudeSdkHarness)

    def test_backend_credential_is_never_resolved_on_the_default_path(self) -> None:
        with patch.object(pyconfig_mod, "resolve_openai_compatible_backend") as resolve_backend:
            harness = resolve_harness(phase="coding")
            assert isinstance(harness, ClaudeSdkHarness)
        resolve_backend.assert_not_called()
