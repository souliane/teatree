import asyncio

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel

from teatree.agents.claude_sdk_session import LiveInputQueue
from teatree.agents.harness import PydanticAiHarness, pydantic_ai_thread
from teatree.agents.live_control import RejectCode, SteerRejectedError
from teatree.agents.pydantic_ai_steering import PeerInputAdmission, SteerablePydanticAiSession


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


def test_the_processor_leaves_a_request_without_waiting_input_untouched() -> None:
    async def run() -> None:
        queue = LiveInputQueue()
        messages = [ModelRequest(parts=[UserPromptPart("go")])]
        assert await PeerInputAdmission(queue)(messages) is messages

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
