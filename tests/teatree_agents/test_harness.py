"""The ``Harness`` seam — backend resolution + the provider-agnostic driver (#2565, #2885).

``resolve_harness`` reads the DB-home ``agent_harness`` setting and returns the
transport backend: the default resolves to :class:`ClaudeSdkHarness`
(byte-identical to the pre-seam transport), ``pydantic_ai`` resolves to
:class:`PydanticAiHarness` (#2885's OrcaRouter-BYOK, OpenAI-compatible backend),
and the ``T3_AGENT_HARNESS`` env / ``ConfigSetting`` store are the switch.
``_drive_with_heartbeat`` talks only to the narrow ``HarnessSession`` surface, so
an arbitrary backend drives a run — both backends yield the SAME
``claude_agent_sdk`` message vocabulary, proved here for ``PydanticAiHarness`` the
same way :class:`FakeHarnessSession` proves it for the generic seam.
"""

import asyncio
import json
import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
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
import teatree.agents.headless as headless_mod
import teatree.agents.pydantic_ai_config as pyconfig_mod
from teatree.agents.harness import (
    ClaudeSdkHarness,
    Harness,
    HarnessSession,
    PydanticAiHarness,
    PydanticAiHarnessSession,
    pydantic_ai_thread,
    resolve_effort,
    resolve_harness,
)
from teatree.agents.harness_options import HarnessOptions
from teatree.agents.headless import LoopWatchdog, TaskUsage, _build_options, _drive_with_heartbeat, run_headless
from teatree.agents.pydantic_ai_config import (
    LANE_BULK,
    LANE_EVAL,
    LANE_FACTORY,
    OrcaLaneConfig,
    PydanticAiModelConfig,
    build_orca_provider,
)
from teatree.agents.pydantic_ai_resume import persist_parked_thread
from teatree.config import get_effective_settings
from teatree.core.models import ConfigSetting, Session, Task, TaskAttempt, Ticket, UsageWindowState
from teatree.llm.credentials import CredentialError, OrcaRouterProviderConfig
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
        # Resolving the backend never itself requires a live OrcaRouter
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
        self.ticket = Ticket.objects.create()
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
        self.ticket = Ticket.objects.create()
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

        with patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))):
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

        with patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))):
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
    """``run_headless`` genuinely dispatches through ``PydanticAiHarness`` when selected."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        self.task = Task.objects.create(ticket=self.ticket, session=self.session, phase="coding")
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")

    def test_pydantic_ai_harness_completes_a_real_run(self) -> None:
        # No `claude` binary check, no Anthropic credential needed — the
        # pydantic_ai harness is injected directly with a TestModel double.
        # ``files_modified`` is the phase-evidence gate's required key for
        # ``coding`` (#1282-6) — unrelated to the harness under test.
        result_json = '{"summary": "test summary", "files_modified": ["a.py"]}'
        fake_harness = PydanticAiHarness(model=TestModel(custom_output_text=result_json))
        with (
            patch.object(headless_mod, "resolve_harness", return_value=fake_harness),
            patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            attempt = run_headless(self.task, phase="coding", overlay_skill_metadata={})

        self.task.refresh_from_db()
        assert attempt.exit_code == 0
        assert self.task.status == Task.Status.COMPLETED
        assert attempt.result["summary"] == "test summary"

    def test_missing_orca_router_credential_records_a_clean_failure(self) -> None:
        # No injected model, no ORCA_ROUTER_BASE_URL/ORCA_ROUTER_API_KEY in the
        # environment — the lazily-resolved CredentialError is caught and
        # recorded, never an uncaught exception.
        with (
            patch.dict(os.environ, {}, clear=False),
            patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            os.environ.pop("ORCA_ROUTER_BASE_URL", None)
            os.environ.pop("ORCA_ROUTER_API_KEY", None)
            attempt = run_headless(self.task, phase="coding", overlay_skill_metadata={})

        self.task.refresh_from_db()
        assert attempt.exit_code == 1
        assert "ORCA_ROUTER" in attempt.error
        assert self.task.status == Task.Status.FAILED
        # Refused before any attempt work beyond the failure record.
        assert TaskAttempt.objects.filter(task=self.task).count() == 1

    def test_missing_credential_on_resume_preserves_the_parked_thread(self) -> None:
        # (souliane/teatree#2916 review) `resolve_harness` pops the parked
        # ancestor's thread as a side effect of BUILDING the harness — before
        # `harness.open()` ever runs, the only point OrcaRouter's credential
        # resolves. A credential failure must restore what it just consumed,
        # or the conversation is lost even though the run never happened.
        from teatree.agents.pydantic_ai_resume import persist_parked_thread  # noqa: PLC0415

        agent = Agent(TestModel(custom_output_text="hi"))
        history = asyncio.run(agent.run("hello")).all_messages()
        parked = Task.objects.create(ticket=self.ticket, session=self.session)
        persist_parked_thread(parked, history)
        resumed_task = Task.objects.create(ticket=self.ticket, session=self.session, phase="coding", parent_task=parked)

        with (
            patch.dict(os.environ, {}, clear=False),
            patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            os.environ.pop("ORCA_ROUTER_BASE_URL", None)
            os.environ.pop("ORCA_ROUTER_API_KEY", None)
            attempt = run_headless(resumed_task, phase="coding", overlay_skill_metadata={})

        resumed_task.refresh_from_db()
        assert attempt.exit_code == 1
        assert "ORCA_ROUTER" in attempt.error
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
            msg = "orca router transport unavailable"
            raise RuntimeError(msg)

        with (
            patch.object(harness_mod.PydanticAiHarness, "_resolve_model", _boom),
            patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
            pytest.raises(RuntimeError, match="orca router transport unavailable"),
        ):
            run_headless(resumed_task, phase="coding", overlay_skill_metadata={})

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
    from teatree.llm.anthropic_limits import classify_limit  # noqa: PLC0415 — test-local

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
    # A genuine FAILED, not a park — its text carries no rate/usage-limit phrase.
    assert classify_limit(str(results[0].result)) is None


def test_pydantic_ai_session_maps_the_request_cap_to_error_max_turns() -> None:
    # A real per-run request cap (request_limit=1) against a model that wants a
    # 2nd request raises UsageLimitExceeded, which the session reports as an
    # is_error ResultMessage(subtype="error_max_turns") — a genuine FAILED whose
    # text does NOT phrase-match the limit classifier (so it never parks).
    from teatree.llm.anthropic_limits import classify_limit  # noqa: PLC0415 — test-local

    agent: Agent[None, str] = Agent(FunctionModel(stream_function=_tool_then_text_stream), toolsets=[_ping_toolset()])
    session = PydanticAiHarnessSession(agent, model_name="m", request_limit=1)

    async def drive() -> list[object]:
        await session.query("hi")
        return [message async for message in session.receive_response()]

    result = next(m for m in asyncio.run(drive()) if isinstance(m, ResultMessage))
    assert result.is_error is True
    assert result.subtype == "error_max_turns"
    assert classify_limit(str(result.result)) is None


class TestRunHeadlessPydanticAiFailureReporting(TestCase):
    """A pydantic_ai provider/run error is REPORTED through the driver, never a raw crash.

    The seam maps the error into the same ``is_error`` ``ResultMessage`` the
    claude_sdk lane yields, so the driver's failure taxonomy (park/rotate or a
    recorded FAILED) fires without any transport special-casing. Before the fix a
    429 propagated raw out of ``asyncio.run`` and ``run_headless`` re-raised it (a
    ``sdk_error`` FAILED-with-traceback), leaving the park path unreachable.
    """

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        self.task = Task.objects.create(ticket=self.ticket, session=self.session, phase="coding")
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")

    def _run_raising(self, exc: Exception) -> TaskAttempt:
        harness = PydanticAiHarness(model=FunctionModel(stream_function=_raising_stream(exc)))
        with (
            patch.object(headless_mod, "resolve_harness", return_value=harness),
            patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            attempt = run_headless(self.task, phase="coding", overlay_skill_metadata={})
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
    harness, bypassing resolution), this drives ``run_headless`` through the
    genuine ``resolve_harness(task)`` seam for BOTH the parking dispatch and
    the resumed continuation — proving the persisted thread actually reaches
    the resumed session's first turn, not just that the plumbing types check.
    """

    def setUp(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        self.task = Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            phase="coding",
            execution_target=Task.ExecutionTarget.HEADLESS,
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
            patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            park_attempt = run_headless(self.task, phase="coding", overlay_skill_metadata={})

        self.task.refresh_from_db()
        assert park_attempt.result["needs_user_input"] is True
        self.ticket.refresh_from_db()
        assert str(self.task.pk) in self.ticket.extra.get("pydantic_ai_threads", {})

        from teatree.core.models.task_handoff import schedule_headless_resume  # noqa: PLC0415

        resumed_task = schedule_headless_resume(self.task, answer="go ahead")

        with (
            patch.object(
                harness_mod.PydanticAiHarness,
                "_resolve_model",
                lambda self, options: FunctionModel(stream_function=stream_fn),
            ),
            patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            resume_attempt = run_headless(resumed_task, phase="coding", overlay_skill_metadata={})

        assert resume_attempt.result["summary"] == "done"
        # The resumed turn's model call carried more messages than a bare
        # first prompt would — the rehydrated park thread landed on it.
        assert captured_message_counts[1] > 1
        self.ticket.refresh_from_db()
        assert str(self.task.pk) not in self.ticket.extra.get("pydantic_ai_threads", {})


class TestPydanticAiHarnessRegulatedPathGate(TestCase):
    """#2887: on a regulated lane, a model off the allowlist never reaches the OrcaRouter provider."""

    def setUp(self) -> None:
        os.environ.pop("ORCA_ROUTER_BASE_URL", None)
        os.environ.pop("ORCA_ROUTER_API_KEY", None)

    def test_model_off_the_allowlist_raises_before_credential_resolution(self) -> None:
        # No OrcaRouter credential configured — proves the regulated-path allowlist
        # check fires FIRST (a config-policy ValueError), not the credential check
        # (which would instead raise CredentialError naming ORCA_ROUTER).
        ConfigSetting.objects.set_value("enforce_regulated_path", value=True)
        ConfigSetting.objects.set_value("regulated_path_model_allowlist", value=["anthropic/"])
        options = ClaudeAgentOptions(model="deepseek/deepseek-v4-pro")

        with pytest.raises(ValueError, match="not eligible for the regulated path"):
            PydanticAiHarness()._resolve_model(options)

    def test_unenforced_lane_reaches_the_credential_step(self) -> None:
        # Default enforce_regulated_path=False — the factory lane is unrestricted,
        # so resolution proceeds to the (here unconfigured) credential step.
        options = ClaudeAgentOptions(model="deepseek/deepseek-v4-pro")

        with pytest.raises(CredentialError, match="ORCA_ROUTER"):
            PydanticAiHarness()._resolve_model(options)

    def test_allowlisted_model_reaches_the_credential_step(self) -> None:
        ConfigSetting.objects.set_value("enforce_regulated_path", value=True)
        ConfigSetting.objects.set_value("regulated_path_model_allowlist", value=["deepseek/"])
        options = ClaudeAgentOptions(model="deepseek/deepseek-v4-pro")

        with pytest.raises(CredentialError, match="ORCA_ROUTER"):
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

    def test_interrupt_cancels_the_underlying_stream_not_just_the_local_task(self) -> None:
        # stream.cancel() (not just cancelling the local drain asyncio.Task)
        # stops token generation, closes the connection, and records the
        # interrupted state — pydantic_ai's own StreamedRunResult.is_complete
        # flips True as a direct side effect of THAT call.
        chunk_seen = asyncio.Event()

        async def slow_stream(_messages: object, _info: AgentInfo) -> AsyncIterator[str]:
            for i in range(50):
                yield f"chunk{i} "
                chunk_seen.set()
                await asyncio.sleep(0.05)

        agent = Agent(FunctionModel(stream_function=slow_stream))
        session = PydanticAiHarnessSession(agent, model_name="test")

        async def drive() -> bool:
            await session.query("hello")
            consumer = asyncio.ensure_future(_collect_all(session))
            await chunk_seen.wait()
            stream = session._active_stream
            assert stream is not None
            await session.interrupt()
            await consumer
            return stream.is_complete

        assert asyncio.run(drive()) is True

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
    """``_resolve_model`` sends OrcaRouter an id its catalog carries (plan §3.2 bug fix)."""

    @pytest.fixture(autouse=True)
    def _orca_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ORCA_ROUTER_BASE_URL", "https://api.orcarouter.ai/v1")
        monkeypatch.setenv("ORCA_ROUTER_API_KEY", "sk-orca-test")

    def test_claude_dash_form_default_is_normalised_to_the_router_handle(self) -> None:
        # The bug: options.model carries a teatree-abstract-tier default in Claude
        # dash-form (claude-opus-4-8), which OrcaRouter does NOT carry. It must be
        # normalised to the router handle, never sent verbatim.
        model = PydanticAiHarness()._resolve_model(HarnessOptions(model="claude-opus-4-8"))
        assert model.model_name == "orcarouter/teatree-factory"

    def test_no_model_pin_resolves_to_the_router_handle(self) -> None:
        model = PydanticAiHarness()._resolve_model(HarnessOptions())
        assert model.model_name == "orcarouter/teatree-factory"

    def test_explicit_orca_native_pin_passes_through(self) -> None:
        model = PydanticAiHarness()._resolve_model(HarnessOptions(model="deepseek/deepseek-v4-pro"))
        assert model.model_name == "deepseek/deepseek-v4-pro"

    def test_a_model_off_the_regulated_allowlist_is_refused(self) -> None:
        ConfigSetting.objects.set_value("enforce_regulated_path", value=True)
        ConfigSetting.objects.set_value("regulated_path_model_allowlist", value=["anthropic/"])
        with pytest.raises(ValueError, match="not eligible for the regulated path"):
            PydanticAiHarness()._resolve_model(HarnessOptions(model="deepseek/deepseek-v4-pro"))


class TestBuildOrcaProvider(TestCase):
    """``_build_orca_provider`` — the OrcaRouter provider + x-lane header (plan §3.4)."""

    @pytest.fixture(autouse=True)
    def _orca_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ORCA_ROUTER_BASE_URL", "https://api.orcarouter.ai/v1")
        monkeypatch.setenv("ORCA_ROUTER_API_KEY", "sk-orca-test")

    def test_factory_lane_rides_the_x_lane_header(self) -> None:
        provider = build_orca_provider(lane=LANE_FACTORY)
        assert provider.client.default_headers["x-lane"] == "factory"
        assert str(provider.client.base_url).rstrip("/") == "https://api.orcarouter.ai/v1"

    def test_eval_lane_rides_the_x_lane_header(self) -> None:
        provider = build_orca_provider(lane=LANE_EVAL)
        assert provider.client.default_headers["x-lane"] == "eval"

    def test_bulk_lane_rides_the_x_lane_header(self) -> None:
        # A secondary overlay's cheap bulk-leg lane: a router DSL rule keys on
        # ``headers["x-lane"] == "bulk"``.
        provider = build_orca_provider(lane=LANE_BULK)
        assert provider.client.default_headers["x-lane"] == "bulk"

    def _capture_pass_path(self, pass_path: str | None) -> str:
        captured: dict[str, str] = {}

        def _spy(*, credential: object) -> object:
            captured["path"] = credential._effective_spec().pass_path
            return OrcaRouterProviderConfig(api_key="sk", base_url="https://api.orcarouter.ai/v1")

        with patch.object(pyconfig_mod, "resolve_orca_router_provider_config", _spy):
            build_orca_provider(lane=LANE_FACTORY, pass_path=pass_path)
        return captured["path"]

    def test_configured_pass_path_is_injected_into_the_credential(self) -> None:
        # The orca_router_pass_path DB-home setting points teatree at an existing
        # per-account pass entry with NO copy (plan §3.6 / task item 4).
        path = "orcarouter/office@example.com/api-key"
        assert self._capture_pass_path(path) == path

    def test_empty_pass_path_has_no_builtin(self) -> None:
        # No built-in default: with no configured orca_router_pass_path the credential's
        # effective pass_path stays None — it resolves from ORCA_ROUTER_API_KEY or fails loud.
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
        assert harness._orca.request_limit == 3

    def test_open_threads_the_request_limit_into_the_session(self) -> None:
        harness = PydanticAiHarness(
            model=TestModel(), config=PydanticAiModelConfig(orca=OrcaLaneConfig(request_limit=4))
        )

        async def drive() -> int | None:
            async with harness.open(ClaudeAgentOptions()) as session:
                assert isinstance(session, PydanticAiHarnessSession)
                return session._request_limit

        assert asyncio.run(drive()) == 4

    def test_positive_max_turns_wins_over_request_limit(self) -> None:
        harness = PydanticAiHarness(
            model=TestModel(), config=PydanticAiModelConfig(orca=OrcaLaneConfig(request_limit=4))
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
            model=TestModel(), config=PydanticAiModelConfig(orca=OrcaLaneConfig(request_limit=4))
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

    def test_default_setting_is_a_conservative_cap(self) -> None:
        assert get_effective_settings().pydantic_ai_request_limit == 5


class TestPydanticAiMaxTokens(TestCase):
    """The per-request ``max_tokens`` ceiling reaches the model settings (binding-agnostic)."""

    def test_default_setting_is_generous(self) -> None:
        assert get_effective_settings().pydantic_ai_max_tokens == 16384

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


class TestOrcaRouterLaneAndRouterNameCallSite(TestCase):
    """The two-router call-site plumbing: config-driven lane + router-handle, resolved TOGETHER.

    ``resolve_harness`` resolves the DB-home ``orca_router_lane`` / ``orca_router_name``
    settings SYNCHRONOUSLY into ``OrcaLaneConfig``, and ``_resolve_model`` binds the
    OrcaRouter base_url + key + router handle + ``x-lane`` header together for the
    selected lane — never a half-swap.
    """

    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_AGENT_HARNESS", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_ORCA_ROUTER_LANE", raising=False)
        monkeypatch.delenv("T3_ORCA_ROUTER_NAME", raising=False)
        monkeypatch.setenv("ORCA_ROUTER_BASE_URL", "https://api.orcarouter.ai/v1")
        monkeypatch.setenv("ORCA_ROUTER_API_KEY", "sk-orca-test")

    def test_router_name_config_threads_into_the_resolved_model(self) -> None:
        # The secondary-router selection: an overlay pointing at its own named router
        # resolves the handle, driven by config — not hardcoded to teatree-factory.
        harness = PydanticAiHarness(
            config=PydanticAiModelConfig(orca=OrcaLaneConfig(router_name="orcarouter/secondary-factory"))
        )
        model = harness._resolve_model(HarnessOptions(model="claude-opus-4-8"))
        assert model.model_name == "orcarouter/secondary-factory"

    def test_default_orca_lane_config_keeps_the_teatree_factory_handle(self) -> None:
        model = PydanticAiHarness()._resolve_model(HarnessOptions(model="claude-opus-4-8"))
        assert model.model_name == "orcarouter/teatree-factory"

    def test_resolve_harness_reads_lane_and_router_name_synchronously(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        ConfigSetting.objects.set_value("orca_router_lane", "bulk")
        ConfigSetting.objects.set_value("orca_router_name", "orcarouter/secondary-factory")
        harness = resolve_harness(phase="coding")
        assert isinstance(harness, PydanticAiHarness)
        assert harness._orca.lane == "bulk"
        assert harness._orca.router_name == "orcarouter/secondary-factory"

    def test_default_lane_is_factory(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        harness = resolve_harness(phase="coding")
        assert isinstance(harness, PydanticAiHarness)
        assert harness._orca.lane == "factory"
        assert harness._orca.router_name is None

    def test_base_url_key_model_and_x_lane_resolve_together_for_the_lane(self) -> None:
        # (a): with OrcaRouter configured, one call binds base_url + key + router
        # handle + x-lane for the right lane — a whole binding, not a half-swap.
        harness = PydanticAiHarness(
            config=PydanticAiModelConfig(
                orca=OrcaLaneConfig(lane=LANE_BULK, router_name="orcarouter/secondary-factory")
            )
        )
        model = harness._resolve_model(HarnessOptions(model="claude-opus-4-8"))
        assert model.model_name == "orcarouter/secondary-factory"
        client = model.client
        assert str(client.base_url).rstrip("/") == "https://api.orcarouter.ai/v1"
        assert client.api_key == "sk-orca-test"
        assert client.default_headers["x-lane"] == "bulk"


class TestOrcaRouterInertByDefault(TestCase):
    """(b): default config → ZERO OrcaRouter involvement. The whole feature ships DARK."""

    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_AGENT_HARNESS", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        # No ORCA_ROUTER_* configured, and no agent_harness row: the default path
        # must never touch OrcaRouter credential/base-url resolution.
        monkeypatch.delenv("ORCA_ROUTER_BASE_URL", raising=False)
        monkeypatch.delenv("ORCA_ROUTER_API_KEY", raising=False)

    def test_default_harness_is_claude_sdk_and_never_resolves_orca(self) -> None:
        for phase in (None, "coding", "planning", "reviewing"):
            with self.subTest(phase=phase):
                assert isinstance(resolve_harness(phase=phase), ClaudeSdkHarness)

    def test_default_settings_do_not_route_to_orca(self) -> None:
        settings = get_effective_settings()
        assert settings.agent_harness.value == "claude_sdk"
        assert settings.orca_router_lane == "factory"
        assert settings.orca_router_name == ""

    def test_building_the_default_harness_makes_no_orca_credential_call(self) -> None:
        # Selecting the default backend must not itself resolve an OrcaRouter
        # credential/base-url — proves the DARK feature stays inert with no key set.
        with patch.object(pyconfig_mod, "resolve_orca_router_provider_config") as spy:
            harness = resolve_harness(phase="coding")
        assert isinstance(harness, ClaudeSdkHarness)
        spy.assert_not_called()


class TestOrcaInertByDefault(TestCase):
    """DEFAULT config keeps every dispatch on claude_sdk — OrcaRouter is inert until enabled."""

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

    def test_orca_credential_is_never_resolved_on_the_default_path(self) -> None:
        with patch.object(pyconfig_mod, "resolve_orca_router_provider_config") as resolve_orca:
            harness = resolve_harness(phase="coding")
            assert isinstance(harness, ClaudeSdkHarness)
        resolve_orca.assert_not_called()
