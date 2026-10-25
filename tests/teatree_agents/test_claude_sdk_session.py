"""Operator input joins a dispatch's hook lists after the gates the dispatch already arms."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from claude_agent_sdk import CLIConnectionError, HookMatcher
from claude_agent_sdk.types import HookEvent, HookJSONOutput, StopHookInput

from teatree.agents.claude_sdk_session import ClaudeSdkSession, LiveInputQueue
from teatree.agents.envelope_stop_gate import EnvelopeStopGate, envelope_stop_hooks
from teatree.agents.live_control import RejectCode, SteerRejectedError
from teatree.agents.subagent_ceiling import SpawnCeiling, spawn_ceiling_hooks
from tests.teatree_agents._sdk_fake import ScriptedResponseClient, assistant_text, result_message


def _dispatch_hooks() -> dict[HookEvent, list[HookMatcher]]:
    return spawn_ceiling_hooks(SpawnCeiling(limit=3)) | envelope_stop_hooks(EnvelopeStopGate("coding", limit=2))


def _stop_answer_with_input_waiting(queue: LiveInputQueue, stop: HookMatcher) -> HookJSONOutput:
    stop_input: StopHookInput = {
        "session_id": "s1",
        "transcript_path": "",
        "cwd": "",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
    }

    async def run() -> HookJSONOutput:
        steering = asyncio.create_task(queue.submit("use docs/x.md", wait=5))
        await asyncio.sleep(0)
        answer = await stop.hooks[0](stop_input, None, {"signal": None})
        await steering
        return answer

    return asyncio.run(run())


def test_the_dispatchs_own_gates_stay_armed_and_run_first() -> None:
    hooks = _dispatch_hooks()
    armed = {event: list(matchers) for event, matchers in hooks.items()}

    merged = LiveInputQueue().hooks_merged_into(hooks)

    assert merged["PreToolUse"] == armed["PreToolUse"]
    assert merged["Stop"][:-1] == armed["Stop"]
    assert len(merged["PostToolUse"]) == 1
    assert hooks == armed


def test_the_appended_stop_hook_delivers_the_waiting_input() -> None:
    queue = LiveInputQueue()

    merged = queue.hooks_merged_into(_dispatch_hooks())

    assert _stop_answer_with_input_waiting(queue, merged["Stop"][-1]) == {
        "decision": "block",
        "reason": "use docs/x.md",
    }


def test_follow_up_rearms_the_input_queue_so_a_steer_reaches_the_new_turn() -> None:
    class Client:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def query(self, prompt: str) -> None:
            self.prompts.append(prompt)

    client = Client()
    inputs = LiveInputQueue()
    inputs.end(RejectCode.TURN_ENDED)
    session = ClaudeSdkSession(client, inputs)

    async def run() -> None:
        await session.follow_up("a second turn")
        steering = asyncio.create_task(session.steer("new turn input", input_id="c-2", wait=1))
        await asyncio.sleep(0)
        assert inputs.take() == "new turn input"
        await steering

    asyncio.run(run())
    assert client.prompts == ["a second turn"]


def test_a_closed_transport_rejects_the_follow_up_without_rearming_input() -> None:
    class ClosedClient:
        async def query(self, prompt: str) -> None:
            message = "closed"
            raise CLIConnectionError(message)

    inputs = LiveInputQueue()
    inputs.end(RejectCode.TURN_ENDED)
    session = ClaudeSdkSession(ClosedClient(), inputs)

    with pytest.raises(SteerRejectedError) as error:
        asyncio.run(session.follow_up("again"))

    assert error.value.code is RejectCode.SESSION_CLOSED
    assert inputs._ended is RejectCode.TURN_ENDED


def test_an_early_clean_zero_turn_result_does_not_hide_the_real_turn() -> None:
    ack = result_message(num_turns=0, session_id="ack")
    answer = assistant_text("real answer")
    result = result_message(num_turns=1, session_id="real")
    client = ScriptedResponseClient([ack, answer, result])
    session = ClaudeSdkSession(client, LiveInputQueue())

    async def run() -> list[object]:
        await session.query("new prompt")
        return await _collect_messages(session)

    assert asyncio.run(run()) == [answer, result]
    assert client.prompts == ["new prompt"]
    assert client.receive_calls == 2


def test_a_clean_zero_turn_result_is_released_once_at_eof() -> None:
    ack = result_message(num_turns=0)
    client = ScriptedResponseClient([ack])
    session = ClaudeSdkSession(client, LiveInputQueue())

    async def run() -> None:
        assert await _collect_messages(session) == [ack]
        with pytest.raises(SteerRejectedError) as error:
            await session.steer("after EOF", input_id="after-eof", wait=1)
        assert error.value.code is RejectCode.TURN_ENDED

    asyncio.run(run())
    assert client.receive_calls == 2


def test_early_ack_hold_yields_to_scheduled_cancellation() -> None:
    ack = result_message(num_turns=0)

    class RepeatingClient(ScriptedResponseClient):
        async def receive_response(self) -> AsyncIterator[object]:
            self.receive_calls += 1
            if self.receive_calls == 1:
                asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
            else:
                raise AssertionError
            yield ack

    client = RepeatingClient([])
    session = ClaudeSdkSession(client, LiveInputQueue())

    async def run() -> None:
        with pytest.raises(asyncio.CancelledError):
            await _collect_messages(session)

    asyncio.run(run())
    assert client.receive_calls == 1


@pytest.mark.parametrize("assistant_before", [False, True])
def test_an_error_or_an_answered_zero_turn_result_ends_immediately(*, assistant_before: bool) -> None:
    result = result_message(num_turns=0, is_error=not assistant_before)
    messages: list[object] = [assistant_text("answer")] if assistant_before else []
    messages += [result, assistant_text("later")]
    client = ScriptedResponseClient(messages)
    session = ClaudeSdkSession(client, LiveInputQueue())

    assert asyncio.run(_collect_messages(session)) == messages[:-1]
    assert client.receive_calls == 1


def test_input_remains_open_between_early_ack_and_real_result() -> None:
    ack = result_message(num_turns=0)
    result = result_message(num_turns=1)
    resumed = asyncio.Event()
    waiting = asyncio.Event()

    class PausedClient(ScriptedResponseClient):
        async def receive_response(self) -> AsyncIterator[object]:
            self.receive_calls += 1
            if self.receive_calls == 1:
                yield ack
                return
            waiting.set()
            await resumed.wait()
            yield result

    client = PausedClient([])
    inputs = LiveInputQueue()
    session = ClaudeSdkSession(client, inputs)

    async def run() -> None:
        collecting = asyncio.create_task(_collect_messages(session))
        await asyncio.wait_for(waiting.wait(), timeout=1)
        steering = asyncio.create_task(session.steer("during turn", input_id="x", wait=1))
        await asyncio.sleep(0)
        assert inputs.take() == "during turn"
        await steering
        resumed.set()
        assert await collecting == [result]
        with pytest.raises(SteerRejectedError) as error:
            await session.steer("after turn", input_id="y", wait=1)
        assert error.value.code is RejectCode.TURN_ENDED

    asyncio.run(run())


async def _collect_messages(session: ClaudeSdkSession) -> list[object]:
    return [message async for message in session.receive_response()]


def test_two_early_acks_release_only_the_latest_at_eof() -> None:
    first = result_message(num_turns=0, session_id="first")
    latest = result_message(num_turns=0, session_id="latest")
    client = ScriptedResponseClient([first, latest])
    session = ClaudeSdkSession(client, LiveInputQueue())

    assert asyncio.run(_collect_messages(session)) == [latest]
    assert client.receive_calls == 3


def test_an_answer_after_an_ack_without_a_result_stays_unfinished() -> None:
    answer = assistant_text("partial")
    client = ScriptedResponseClient([result_message(num_turns=0), answer])
    session = ClaudeSdkSession(client, LiveInputQueue())

    assert asyncio.run(_collect_messages(session)) == [answer]
    assert client.receive_calls == 2


def test_follow_up_uses_a_fresh_response_guard() -> None:
    first = result_message(num_turns=1, session_id="first")
    ack = result_message(num_turns=0, session_id="ack")
    answer = assistant_text("second answer")
    second = result_message(num_turns=1, session_id="second")
    client = ScriptedResponseClient([first, ack, answer, second])
    session = ClaudeSdkSession(client, LiveInputQueue())

    async def run() -> None:
        await session.query("first prompt")
        assert await _collect_messages(session) == [first]
        await session.follow_up("second prompt")
        assert await _collect_messages(session) == [answer, second]

    asyncio.run(run())
    assert client.prompts == ["first prompt", "second prompt"]


def test_stream_without_result_does_not_reenter_the_sdk_reader() -> None:
    partial = assistant_text("partial")
    client = ScriptedResponseClient([partial])
    session = ClaudeSdkSession(client, LiveInputQueue())

    assert asyncio.run(_collect_messages(session)) == [partial]
    assert client.receive_calls == 1
