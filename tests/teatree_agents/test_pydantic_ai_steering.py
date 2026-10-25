import asyncio
from dataclasses import replace

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage
from django.test import TestCase
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel

from teatree.agents.claude_sdk_session import LiveInputQueue
from teatree.agents.harness import PydanticAiHarness, pydantic_ai_thread
from teatree.agents.live_control import ControlOutcome, FollowUpSession, LiveTask, RejectCode, SteerRejectedError
from teatree.agents.live_registry import LiveSessionRegistry
from teatree.agents.pydantic_ai_resume import persist_parked_thread, rehydrate_thread_for_resume
from teatree.agents.pydantic_ai_steering import SteerablePydanticAiSession
from teatree.agents.runner_stream import StreamCapture
from teatree.core.models import Session, Task
from tests.factories import planned_ticket


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


def test_an_idle_pydantic_session_takes_a_follow_up_as_a_new_turn() -> None:
    async def run() -> tuple[str, list[str]]:
        requests: list[str] = []

        async def model(messages: object, _info: AgentInfo) -> object:
            requests.append(repr(messages))
            await asyncio.sleep(0)
            yield "done"

        harness = PydanticAiHarness(model=FunctionModel(stream_function=model))
        async with harness.open(ClaudeAgentOptions()) as session:
            assert isinstance(session, SteerablePydanticAiSession)
            assert isinstance(session, FollowUpSession)
            with pytest.raises(SteerRejectedError) as early:
                await session.follow_up("too early")
            assert early.value.code == RejectCode.NOT_IDLE
            await session.query("first turn")
            await _collect(session)
            await session.follow_up("second turn")
            await _collect(session)
            session_id = session.session_id
        with pytest.raises(SteerRejectedError) as closed:
            await session.follow_up("too late")
        assert closed.value.code == RejectCode.SESSION_CLOSED
        return session_id, requests

    session_id, requests = asyncio.run(run())

    assert session_id
    assert len(requests) == 2
    assert "first turn" in requests[0]
    assert "first turn" in requests[1]
    assert "second turn" in requests[1]


def test_reopen_clears_only_a_finished_turn_and_preserves_first_end_reason() -> None:
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
        with pytest.raises(SteerRejectedError) as error:
            await queue.submit("too late", wait=1)
        assert error.value.code == RejectCode.TURN_ENDED

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


def test_input_during_the_final_request_redirects_into_the_same_run() -> None:
    async def run() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        requests: list[str] = []

        async def model(messages: object, _info: AgentInfo) -> object:
            requests.append(repr(messages))
            if len(requests) == 1:
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
            await steering
            assert len(requests) == 2
            assert "late" in requests[1]

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


class AdmittedTextResumeTests(TestCase):
    def test_a_resume_rehydrates_the_peer_text_admitted_in_the_parked_run(self) -> None:
        async def run() -> list[ModelMessage]:
            entered = asyncio.Event()
            release = asyncio.Event()
            requests = 0

            async def stream_fn(_messages: object, _info: AgentInfo) -> object:
                nonlocal requests
                requests += 1
                if requests == 1:
                    entered.set()
                    await release.wait()
                    yield {0: DeltaToolCall(name="no_such_tool", json_args="{}", tool_call_id="c1")}
                else:
                    yield "done"

            harness = PydanticAiHarness(model=FunctionModel(stream_function=stream_fn))
            async with harness.open(ClaudeAgentOptions()) as live:
                assert isinstance(live, SteerablePydanticAiSession)
                await live.query("go")
                turn = asyncio.create_task(_collect(live))
                await entered.wait()
                steering = asyncio.create_task(live.steer("resumable nonce", input_id="n1", wait=2))
                await asyncio.sleep(0)
                release.set()
                await steering
                await turn
                return live.history

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
