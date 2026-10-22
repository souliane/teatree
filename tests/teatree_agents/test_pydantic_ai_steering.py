import asyncio
import threading
from dataclasses import replace

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage
from django.test import TestCase
from pydantic_ai import Agent
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel

from teatree.agents.claude_sdk_session import LiveInputQueue
from teatree.agents.harness import PydanticAiHarness, pydantic_ai_thread
from teatree.agents.live_control import ControlOutcome, LiveTask, RejectCode, SteerRejectedError
from teatree.agents.live_registry import LiveSessionRegistry
from teatree.agents.pydantic_ai_resume import persist_parked_thread, rehydrate_thread_for_resume
from teatree.agents.pydantic_ai_steering import PeerInputAdmission, SteerablePydanticAiSession
from teatree.agents.runner_stream import StreamCapture
from teatree.core.models import Session, Task
from tests.factories import planned_ticket


def test_a_steer_is_admitted_into_the_next_model_request_and_acknowledged_only_then() -> None:
    async def run() -> None:
        queue = LiveInputQueue()
        processor = PeerInputAdmission(queue)
        pending = asyncio.create_task(queue.submit("peer nonce", wait=1))
        await asyncio.sleep(0)
        assert not pending.done()

        original = [ModelRequest(parts=[UserPromptPart("go")])]
        admitted = await processor(original)

        await pending
        assert admitted is not original
        assert admitted[0].parts[0] == original[0].parts[0]
        assert [part.content for part in admitted[0].parts if isinstance(part, UserPromptPart)] == ["go", "peer nonce"]

    asyncio.run(run())


def test_an_expired_wait_never_enters_a_later_request() -> None:
    async def run() -> None:
        queue = LiveInputQueue()
        with pytest.raises(SteerRejectedError) as error:
            await queue.submit("expired", wait=0)
        assert error.value.code == RejectCode.NOT_ACCEPTED_IN_TIME
        admitted = await PeerInputAdmission(queue)([ModelRequest(parts=[UserPromptPart("go")])])
        assert [part.content for part in admitted[0].parts if isinstance(part, UserPromptPart)] == ["go"]

    asyncio.run(run())


def test_a_running_pydantic_ai_session_admits_input_before_its_next_request() -> None:
    async def run() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        requests: list[str] = []

        async def stream_fn(messages: object, _info: AgentInfo) -> object:
            requests.append(repr(messages))
            if len(requests) == 1:
                entered.set()
                await release.wait()
                yield {0: DeltaToolCall(name="Bash", json_args='{"command":"echo ok"}', tool_call_id="c1")}
            else:
                yield "done"

        harness = PydanticAiHarness(model=FunctionModel(stream_function=stream_fn), phase="coding")
        async with harness.open(ClaudeAgentOptions()) as session:
            assert isinstance(session, SteerablePydanticAiSession)
            await session.query("go")
            turn = asyncio.create_task(_collect(session))
            await asyncio.wait_for(entered.wait(), timeout=2)
            steering = asyncio.create_task(session.steer("peer nonce", input_id="n1", wait=2))
            await asyncio.sleep(0)
            assert not steering.done()
            release.set()
            await steering
            assert any(isinstance(message, ResultMessage) for message in await turn)
            assert "peer nonce" not in requests[0]
            assert requests[1].count("peer nonce") == 1
            assert "peer nonce" in repr(pydantic_ai_thread(session))

    asyncio.run(run())


async def _collect(session: SteerablePydanticAiSession) -> list[object]:
    return [message async for message in session.receive_response()]


def test_reopen_clears_only_a_finished_turn_and_close_is_final() -> None:
    async def run() -> None:
        queue = LiveInputQueue()
        queue.end(RejectCode.TURN_ENDED)
        queue.reopen()
        pending = asyncio.create_task(queue.submit("second turn", wait=1))
        await asyncio.sleep(0)
        assert queue.take() == "second turn"
        await pending
        queue.end(RejectCode.TURN_ENDED)
        queue.end(RejectCode.SESSION_CLOSED)
        queue.reopen()
        with pytest.raises(SteerRejectedError) as error:
            await queue.submit("too late", wait=1)
        assert error.value.code == RejectCode.SESSION_CLOSED

    asyncio.run(run())


def test_input_after_the_turn_or_after_close_is_refused() -> None:
    async def run() -> None:
        harness = PydanticAiHarness(model=TestModel(custom_output_text="done"))
        async with harness.open(ClaudeAgentOptions()) as session:
            assert isinstance(session, SteerablePydanticAiSession)
            await session.query("go")
            await _collect(session)
            with pytest.raises(SteerRejectedError) as ended:
                await session.steer("late", input_id="late", wait=1)
            assert ended.value.code == RejectCode.TURN_ENDED
        with pytest.raises(SteerRejectedError) as closed:
            await session.steer("later", input_id="later", wait=1)
        assert closed.value.code == RejectCode.SESSION_CLOSED

    asyncio.run(run())


def test_input_pending_during_the_final_request_is_refused_when_the_turn_ends() -> None:
    async def run() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def model(_messages: object, _info: AgentInfo) -> object:
            entered.set()
            await release.wait()
            yield "done"

        harness = PydanticAiHarness(model=FunctionModel(stream_function=model))
        async with harness.open(ClaudeAgentOptions()) as session:
            assert isinstance(session, SteerablePydanticAiSession)
            await session.query("go")
            running = asyncio.create_task(_collect(session))
            await asyncio.wait_for(entered.wait(), timeout=2)
            steering = asyncio.create_task(session.steer("late", input_id="late", wait=2))
            await asyncio.sleep(0)
            assert not steering.done()
            release.set()
            await running
            with pytest.raises(SteerRejectedError) as ended:
                await steering
            assert ended.value.code == RejectCode.TURN_ENDED

    asyncio.run(run())


def test_provider_failure_refuses_pending_input_as_turn_ended() -> None:
    async def run() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def model(_messages: object, _info: AgentInfo) -> object:
            entered.set()
            await release.wait()
            yield "partial"
            message = "provider failed"
            raise RuntimeError(message)

        harness = PydanticAiHarness(model=FunctionModel(stream_function=model))
        async with harness.open(ClaudeAgentOptions()) as session:
            await session.query("go")
            running = asyncio.create_task(_collect(session))
            await asyncio.wait_for(entered.wait(), timeout=2)
            steering = asyncio.create_task(session.steer("late", input_id="late", wait=0.2))
            await asyncio.sleep(0)
            assert not steering.done()
            release.set()
            with pytest.raises(RuntimeError, match="provider failed"):
                await running
            with pytest.raises(SteerRejectedError) as ended:
                await steering
            assert ended.value.code == RejectCode.TURN_ENDED

    asyncio.run(run())


def test_several_waiting_messages_are_admitted_once_each_in_arrival_order() -> None:
    async def run() -> None:
        queue = LiveInputQueue()
        first = asyncio.create_task(queue.submit("first nonce", wait=1))
        second = asyncio.create_task(queue.submit("second nonce", wait=1))
        await asyncio.sleep(0)
        processor = PeerInputAdmission(queue)

        admitted = await processor([ModelRequest(parts=[UserPromptPart("go")])])
        await asyncio.gather(first, second)

        appended = [part.content for part in admitted[0].parts if isinstance(part, UserPromptPart)][1:]
        assert len(appended) == 1
        assert appended[0].count("first nonce") == appended[0].count("second nonce") == 1
        assert appended[0].index("first nonce") < appended[0].index("second nonce")
        assert await processor(admitted) is admitted

    asyncio.run(run())


def test_a_second_turn_of_one_session_accepts_input_again() -> None:
    async def run() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        requests: list[str] = []

        async def stream_fn(messages: object, _info: AgentInfo) -> object:
            requests.append(repr(messages))
            if len(requests) == 2:
                entered.set()
                await release.wait()
                yield {0: DeltaToolCall(name="Bash", json_args='{"command":"echo ok"}', tool_call_id="c2")}
            else:
                yield "done"

        harness = PydanticAiHarness(model=FunctionModel(stream_function=stream_fn), phase="coding")
        async with harness.open(ClaudeAgentOptions()) as session:
            await session.query("one")
            await _collect(session)
            await session.query("two")
            turn = asyncio.create_task(_collect(session))
            await asyncio.wait_for(entered.wait(), timeout=2)
            steering = asyncio.create_task(session.steer("second turn nonce", input_id="n2", wait=2))
            await asyncio.sleep(0)
            release.set()
            await steering
            await turn
            assert "second turn nonce" not in requests[1]
            assert requests[2].count("second turn nonce") == 1

    asyncio.run(run())


def test_admission_does_not_depend_on_the_dispatch_phase() -> None:
    async def run() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        requests: list[str] = []

        async def stream_fn(messages: object, _info: AgentInfo) -> object:
            requests.append(repr(messages))
            if len(requests) == 1:
                entered.set()
                await release.wait()
                yield {0: DeltaToolCall(name="no_such_tool", json_args="{}", tool_call_id="c1")}
            else:
                yield "done"

        harness = PydanticAiHarness(model=FunctionModel(stream_function=stream_fn))
        async with harness.open(ClaudeAgentOptions()) as session:
            await session.query("go")
            turn = asyncio.create_task(_collect(session))
            await asyncio.wait_for(entered.wait(), timeout=2)
            steering = asyncio.create_task(session.steer("unphased nonce", input_id="n1", wait=2))
            await asyncio.sleep(0)
            release.set()
            await steering
            await turn
            assert requests[1].count("unphased nonce") == 1

    asyncio.run(run())


def test_the_processor_runs_on_the_session_loop() -> None:
    class _RecordingQueue(LiveInputQueue):
        def __init__(self) -> None:
            super().__init__()
            self.threads: list[int] = []

        def take(self) -> str:
            self.threads.append(threading.get_ident())
            return super().take()

    queue = _RecordingQueue()
    agent: Agent[None, str] = Agent(
        TestModel(custom_output_text="done"), capabilities=[ProcessHistory(PeerInputAdmission(queue))]
    )

    asyncio.run(agent.run("go"))

    assert queue.threads == [threading.get_ident()]


class AdmittedTextResumeTests(TestCase):
    def test_a_resume_rehydrates_the_peer_text_admitted_in_the_parked_run(self) -> None:
        async def run() -> list[ModelMessage]:
            queue = LiveInputQueue()
            agent: Agent[None, str] = Agent(
                TestModel(custom_output_text="done"), capabilities=[ProcessHistory(PeerInputAdmission(queue))]
            )
            pending = asyncio.create_task(queue.submit("resumable nonce", wait=1))
            await asyncio.sleep(0)
            result = await agent.run("go")
            await pending
            return result.all_messages()

        ticket = planned_ticket()
        session = Session.objects.create(ticket=ticket)
        parked = Task.objects.create(ticket=ticket, session=session)
        persist_parked_thread(parked, asyncio.run(run()))

        child = Task.objects.create(
            ticket=ticket, session=session, parent_task=parked, session_continuation=Task.SessionContinuation.PARENT
        )

        resumed = rehydrate_thread_for_resume(child)

        assert resumed is not None
        assert "resumable nonce" in repr(resumed.history)


@pytest.mark.parametrize("stale_field", ["attempt", "generation"])
def test_a_stale_command_never_reaches_a_pydantic_session(stale_field: str) -> None:
    async def run() -> None:
        registry = LiveSessionRegistry()
        task = LiveTask(pk=9, ticket=1, phase="coding", harness="pydantic_ai", model="function")
        fence = task.fence
        stale = replace(fence, **{stale_field: f"{getattr(fence, stale_field)}-older"})
        harness = PydanticAiHarness(model=TestModel(custom_output_text="done"))
        async with harness.open(ClaudeAgentOptions()) as session, registry.attach(task, StreamCapture()) as live:
            live.bind(session)
            steered = await registry.steer(9, "stale steer", command_id="s", wait=0.1, fence=stale)
            pushed = await registry.push(9, "stale push", command_id="p", wait=0.1, fence=stale)
            for receipt in (steered, pushed):
                assert (receipt.outcome, receipt.code) == (ControlOutcome.REJECTED, RejectCode("stale_command"))
            facts = registry.facts(9)
            assert facts is not None
            assert facts["pending_inputs"] == 0

    asyncio.run(run())
