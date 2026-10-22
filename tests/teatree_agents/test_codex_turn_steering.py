"""Codex current-turn admission is decided by the App Server reply."""

import asyncio
import json
from pathlib import Path
from typing import Any, get_args, get_type_hints
from unittest.mock import AsyncMock, Mock

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents.codex_app_server import CodexAppServerSession
from teatree.agents.codex_app_server_errors import request_error
from teatree.agents.codex_app_server_options import CodexAppServerError, CodexAppServerOptions
from teatree.agents.codex_read_only_server import executor_session
from teatree.agents.codex_shared_app_server import SharedCodexAppServer, SharedCodexSession
from teatree.agents.codex_turn_steering import CodexSteerUnknownError
from teatree.agents.live_control import RejectCode, SteerableSession, SteerRejectedError

_CONTRACT = Path(__file__).parents[1] / "fixtures/codex_app_server/0.155.1-contract.json"


def _session(tmp_path: Path, answer: dict[str, Any] | None = None) -> tuple[CodexAppServerSession, AsyncMock]:
    options = CodexAppServerOptions.from_sdk_options(ClaudeAgentOptions(cwd=str(tmp_path)))
    session = CodexAppServerSession(options, resume=None, code_home=tmp_path)
    session.thread_id = "thread-1"

    async def reply(method: str, params: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0)
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        return answer if answer is not None else {"turnId": "turn-1"}

    request = AsyncMock(side_effect=reply)
    session._request = request
    return session, request


def test_steer_writes_turn_steer_with_the_open_turn_id_and_reports_admission(tmp_path: Path) -> None:
    session, request = _session(tmp_path)

    async def run() -> None:
        await session.query("start")
        await session.steer("nonce", input_id="send-1", wait=1)

    asyncio.run(run())
    params = request.call_args.args[1]
    assert request.call_args.args[0] == "turn/steer"
    assert params == {
        "threadId": "thread-1",
        "expectedTurnId": "turn-1",
        "input": [{"type": "text", "text": "nonce"}],
    }
    method = json.loads(_CONTRACT.read_text())["methods"]["turn/steer"]
    assert set(method["required"]) <= params.keys() <= set(method["allowed"])


@pytest.mark.parametrize("answer", [{"turnId": "other"}, {}])
def test_a_success_for_another_turn_id_is_unknown_delivery(tmp_path: Path, answer: dict[str, Any]) -> None:
    session, _ = _session(tmp_path, answer)

    async def run() -> None:
        await session.query("start")
        with pytest.raises(CodexSteerUnknownError) as caught:
            await session.steer("nonce", input_id="send-1", wait=1)
        assert type(caught.value).__name__ == "CodexSteerUnknownError"

    asyncio.run(run())


@pytest.mark.parametrize(
    ("error", "code"),
    [
        ({"code": -32000, "data": {"codexErrorInfo": {"activeTurnNotSteerable": {}}}}, RejectCode.NOT_STEERABLE),
        ({"code": -32601}, RejectCode.NOT_STEERABLE),
        ({"code": -32600}, RejectCode.TURN_ENDED),
    ],
)
def test_server_refusals_are_typed_rejections(error: dict[str, Any], code: RejectCode) -> None:
    caught = request_error("turn/steer", error)
    assert isinstance(caught, SteerRejectedError)
    assert caught.code is code


def test_an_unrecognised_server_error_is_unknown_delivery() -> None:
    caught = request_error("turn/steer", {"code": -32000, "message": "do-not-print"})
    assert not isinstance(caught, SteerRejectedError)


def test_an_invalid_request_with_codex_error_info_is_unknown_delivery() -> None:
    caught = request_error("turn/steer", {"code": -32600, "data": {"codexErrorInfo": {"other": {}}}})
    assert not isinstance(caught, SteerRejectedError)


def test_no_answer_in_time_is_unknown_delivery_not_not_accepted_in_time(tmp_path: Path) -> None:
    session, request = _session(tmp_path)

    async def hang(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        await asyncio.Event().wait()
        return {}

    request.side_effect = hang

    async def run() -> None:
        await session.query("start")
        with pytest.raises(CodexSteerUnknownError) as caught:
            await session.steer("nonce", input_id="send-1", wait=0.01)
        assert type(caught.value).__name__ == "CodexSteerUnknownError"

    asyncio.run(run())


def test_a_steer_racing_the_turn_end_is_unknown_and_written_once(tmp_path: Path) -> None:
    session, request = _session(tmp_path)
    release = asyncio.Event()
    written = asyncio.Event()

    async def reply(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        written.set()
        await release.wait()
        return {"turnId": "turn-1"}

    request.side_effect = reply

    async def run() -> None:
        await session.query("start")
        steering = asyncio.create_task(session.steer("nonce", input_id="send-1", wait=1))
        await written.wait()
        session._turn_closed()
        with pytest.raises(CodexSteerUnknownError) as caught:
            await asyncio.wait_for(steering, 0.2)
        assert type(caught.value).__name__ == "CodexSteerUnknownError"
        release.set()

    asyncio.run(run())
    assert [call.args[0] for call in request.call_args_list] == ["turn/start", "turn/steer"]


def test_transport_loss_after_the_steer_write_is_unknown_without_replay(tmp_path: Path) -> None:
    session, request = _session(tmp_path)

    async def lost(method: str, params: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0)
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        raise CodexAppServerError.stopped()

    request.side_effect = lost

    async def run() -> None:
        await session.query("start")
        with pytest.raises(CodexSteerUnknownError):
            await session.steer("nonce", input_id="send-1", wait=1)

    asyncio.run(run())
    assert [call.args[0] for call in request.call_args_list] == ["turn/start", "turn/steer"]


def test_close_with_a_pending_steer_is_unknown_without_replay(tmp_path: Path) -> None:
    session, request = _session(tmp_path)
    written = asyncio.Event()

    async def hang(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        written.set()
        await asyncio.Event().wait()
        return {}

    request.side_effect = hang

    async def run() -> None:
        await session.query("start")
        steering = asyncio.create_task(session.steer("nonce", input_id="send-1", wait=1))
        await written.wait()
        await session.close()
        with pytest.raises(CodexSteerUnknownError):
            await asyncio.wait_for(steering, 0.2)

    asyncio.run(run())
    assert [call.args[0] for call in request.call_args_list] == ["turn/start", "turn/steer"]


def test_receive_response_closes_steering_when_the_turn_completes(tmp_path: Path) -> None:
    session, request = _session(tmp_path)
    session._next_event = AsyncMock(
        return_value={
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed", "items": []}},
        }
    )

    async def run() -> None:
        await session.query("start")
        async for _ in session.receive_response():
            pass
        with pytest.raises(SteerRejectedError) as caught:
            await session.steer("late", input_id="late-1", wait=0.1)
        assert caught.value.code is RejectCode.TURN_ENDED

    asyncio.run(run())
    assert [call.args[0] for call in request.call_args_list] == ["turn/start"]


def test_app_server_close_marks_steering_closed(tmp_path: Path) -> None:
    session, request = _session(tmp_path)

    async def run() -> None:
        await session.query("start")
        await session.close()
        with pytest.raises(SteerRejectedError) as caught:
            await session.steer("late", input_id="late-1", wait=0.1)
        assert caught.value.code is RejectCode.SESSION_CLOSED

    asyncio.run(run())
    assert [call.args[0] for call in request.call_args_list] == ["turn/start"]


def test_shared_session_close_marks_steering_closed(tmp_path: Path) -> None:
    options = CodexAppServerOptions.from_sdk_options(ClaudeAgentOptions(cwd=str(tmp_path)))
    manager = Mock(spec=SharedCodexAppServer)
    manager.code_home = tmp_path
    manager.request = AsyncMock(
        side_effect=lambda _options, method, _params: (
            {"turn": {"id": "turn-1"}} if method == "turn/start" else {"turnId": "turn-1"}
        )
    )
    manager.close_session = AsyncMock()
    session = SharedCodexSession(options, manager=manager, resume=None)
    session.thread_id = "thread-1"

    async def run() -> None:
        await session.query("start")
        await session.close()
        with pytest.raises(SteerRejectedError) as caught:
            await session.steer("late", input_id="late-1", wait=0.1)
        assert caught.value.code is RejectCode.SESSION_CLOSED

    asyncio.run(run())
    manager.close_session.assert_awaited_once()
    assert manager.request.await_count == 1


def test_two_racing_steers_are_answered_independently_and_never_replayed(tmp_path: Path) -> None:
    session, request = _session(tmp_path)
    first: asyncio.Future[dict[str, Any]]
    second: asyncio.Future[dict[str, Any]]

    async def run() -> None:
        nonlocal first, second
        first = asyncio.get_running_loop().create_future()
        second = asyncio.get_running_loop().create_future()
        written = asyncio.Event()

        async def reply(method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "turn/start":
                return {"turn": {"id": "turn-1"}}
            if request.call_count == 3:
                written.set()
            return await (first if params["input"][0]["text"] == "first" else second)

        request.side_effect = reply
        await session.query("start")
        a = asyncio.create_task(session.steer("first", input_id="send-1", wait=1))
        b = asyncio.create_task(session.steer("second", input_id="send-2", wait=1))
        await written.wait()
        second.set_result({"turnId": "turn-1"})
        first.set_exception(SteerRejectedError(RejectCode.NOT_STEERABLE))
        await b
        with pytest.raises(SteerRejectedError) as caught:
            await a
        assert caught.value.code is RejectCode.NOT_STEERABLE

    asyncio.run(run())
    assert [call.args[0] for call in request.call_args_list] == ["turn/start", "turn/steer", "turn/steer"]


def test_idle_ended_and_closed_sessions_refuse_without_writing(tmp_path: Path) -> None:
    session, request = _session(tmp_path)

    async def run() -> None:
        with pytest.raises(SteerRejectedError) as idle:
            await session.steer("nonce", input_id="send-1", wait=1)
        assert idle.value.code is RejectCode.TURN_ENDED
        await session.query("start")
        session._turn_closed()
        with pytest.raises(SteerRejectedError) as ended:
            await session.steer("nonce", input_id="send-2", wait=1)
        assert ended.value.code is RejectCode.TURN_ENDED
        session._turn_closed(session=True)
        with pytest.raises(SteerRejectedError) as closed:
            await session.steer("nonce", input_id="send-3", wait=1)
        assert closed.value.code is RejectCode.SESSION_CLOSED

    asyncio.run(run())
    assert [call.args[0] for call in request.call_args_list] == ["turn/start"]


def test_the_production_shared_session_and_the_executor_class_are_steerable() -> None:
    assert issubclass(SharedCodexSession, SteerableSession)
    assert get_args(get_type_hints(executor_session)["return"])[0] is SharedCodexSession
