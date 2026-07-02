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
import os
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock
from django.test import TestCase
from pydantic_ai import Agent
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

import teatree.agents.headless as headless_mod
from teatree.agents.harness import (
    ClaudeSdkHarness,
    Harness,
    HarnessSession,
    PydanticAiHarness,
    PydanticAiHarnessSession,
    _extract_system_prompt,
    _resolve_effort,
    resolve_harness,
)
from teatree.agents.headless import LoopWatchdog, TaskUsage, _build_options, _drive_with_heartbeat, run_headless
from teatree.config import get_effective_settings
from teatree.core.models import ConfigSetting, Session, Task, TaskAttempt, Ticket
from teatree.llm.credentials import CredentialError
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


class TestPydanticAiHarnessChineseModelGate(TestCase):
    """#2887: a disallowed Chinese-origin model never reaches the OrcaRouter provider."""

    def setUp(self) -> None:
        os.environ.pop("ORCA_ROUTER_BASE_URL", None)
        os.environ.pop("ORCA_ROUTER_API_KEY", None)

    def test_disallowed_chinese_model_raises_before_credential_resolution(self) -> None:
        # No OrcaRouter credential configured — proves the Chinese-model check
        # fires FIRST (a config-policy ValueError), not the credential check
        # (which would instead raise CredentialError naming ORCA_ROUTER).
        ConfigSetting.objects.set_value("chinese_models_allowed", value=False)
        harness = PydanticAiHarness()
        options = ClaudeAgentOptions(model="deepseek-v3")

        with pytest.raises(ValueError, match="Chinese-origin"):
            harness._resolve_model(options)

    def test_disallowed_setting_does_not_block_a_non_chinese_model(self) -> None:
        ConfigSetting.objects.set_value("chinese_models_allowed", value=False)
        harness = PydanticAiHarness()
        options = ClaudeAgentOptions()  # falls back to the default (Claude) tier

        # No Chinese-origin model involved, so resolution proceeds to the
        # (here unconfigured) credential step instead of the allowlist gate.
        with pytest.raises(CredentialError, match="ORCA_ROUTER"):
            harness._resolve_model(options)

    def test_chinese_model_allowed_reaches_the_credential_step(self) -> None:
        ConfigSetting.objects.set_value("chinese_models_allowed", value=True)
        harness = PydanticAiHarness()
        options = ClaudeAgentOptions(model="deepseek-v3")

        with pytest.raises(CredentialError, match="ORCA_ROUTER"):
            harness._resolve_model(options)


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


class TestExtractSystemPrompt:
    def test_plain_string_passes_through(self) -> None:
        options = ClaudeAgentOptions(system_prompt="a plain prompt")
        assert _extract_system_prompt(options) == "a plain prompt"

    def test_preset_extracts_the_appended_context(self) -> None:
        options = ClaudeAgentOptions(
            system_prompt={"type": "preset", "preset": "claude_code", "append": "the appended context"}
        )
        assert _extract_system_prompt(options) == "the appended context"

    def test_none_yields_empty_string(self) -> None:
        options = ClaudeAgentOptions(system_prompt=None)
        assert _extract_system_prompt(options) == ""


class TestResolveEffort:
    def test_valid_shared_effort_passes_through(self) -> None:
        options = ClaudeAgentOptions(effort="xhigh")
        assert _resolve_effort(options) == "xhigh"

    def test_claude_only_max_is_dropped(self) -> None:
        # "max" is on claude_sdk's EFFORT_SCALE but not pydantic_ai's
        # ReasoningEffort vocabulary — the harness must never forward it.
        options = ClaudeAgentOptions(effort="max")
        assert _resolve_effort(options) is None

    def test_absent_effort_is_none(self) -> None:
        options = ClaudeAgentOptions(effort=None)
        assert _resolve_effort(options) is None
