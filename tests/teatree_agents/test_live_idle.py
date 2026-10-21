"""Only a clean capable session enters the bounded idle window."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from time import monotonic
from typing import cast

import pytest

from teatree.agents.claude_sdk_session import ClaudeSdkSession
from teatree.agents.codex_app_server import CodexAppServerSession
from teatree.agents.codex_shared_app_server import SharedCodexSession
from teatree.agents.harness import HarnessSession, PydanticAiHarnessSession
from teatree.agents.live_control import ControlOutcome, FollowUpSession, LiveSessionController, LiveTask, RejectCode
from teatree.agents.live_idle import IdleWindowBudget, can_idle, hold_idle_window
from teatree.agents.runner_stream import StreamCapture
from tests.teatree_agents._sdk_fake import assistant_text, result_message


class _CapableSession:
    async def follow_up(self, prompt: str) -> None:
        return None


def test_only_the_claude_session_class_supports_a_follow_up_turn() -> None:
    assert issubclass(ClaudeSdkSession, FollowUpSession)
    assert not issubclass(PydanticAiHarnessSession, FollowUpSession)
    assert not issubclass(CodexAppServerSession, FollowUpSession)
    assert not issubclass(SharedCodexSession, FollowUpSession)


def _controller(capture: StreamCapture) -> LiveSessionController:
    live = LiveSessionController(LiveTask(7, 3, "coding", "claude_sdk", "m"), capture)
    live.bind(_CapableSession())
    return live


def test_a_clean_capable_result_can_enter_idle() -> None:
    capture = StreamCapture()
    capture.observe(result_message())
    live = _controller(capture)

    assert can_idle(_CapableSession(), capture.outcome(), live)


def test_an_error_result_or_round_handoff_cannot_enter_idle() -> None:
    capture = StreamCapture()
    capture.observe(result_message(is_error=True))
    live = _controller(capture)

    assert not can_idle(_CapableSession(), capture.outcome(), live)
    assert not can_idle(_CapableSession(), replace(capture.outcome(), round_handoff="next"), live)


@pytest.mark.parametrize("unfit", [{"stuck_reason": "wedged"}, {"compaction_stopped": True}, {"result_message": None}])
def test_a_stuck_compaction_stopped_or_resultless_run_cannot_enter_idle(unfit: dict[str, object]) -> None:
    capture = StreamCapture()
    capture.observe(result_message())
    live = _controller(capture)

    assert not can_idle(_CapableSession(), replace(capture.outcome(), **unfit), live)


def test_a_closed_controller_cannot_enter_idle() -> None:
    capture = StreamCapture()
    capture.observe(result_message())
    live = _controller(capture)
    live.close()

    assert not can_idle(_CapableSession(), capture.outcome(), live)


def test_a_claim_made_before_the_deadline_is_drained_and_a_later_command_is_session_closed() -> None:
    class SlowStartSession(_CapableSession):
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def follow_up(self, prompt: str) -> None:
            await asyncio.sleep(0.15)
            self.prompts.append(prompt)

        async def receive_response(self) -> AsyncIterator[object]:
            yield assistant_text("second turn")
            yield result_message(result="second turn")

    session = SlowStartSession()
    capture = StreamCapture()
    capture.observe(result_message())
    live = _controller(capture)
    live.bind(session)

    async def run() -> tuple[ControlOutcome, str, RejectCode | None]:
        held = asyncio.create_task(
            hold_idle_window(
                cast("HarnessSession", session), capture, live, capture.outcome(), budget=IdleWindowBudget(0.1, None)
            )
        )
        await asyncio.sleep(0.02)
        receipt = await live.follow_up("claimed in time", command_id="in-time", wait=1)
        outcome = await held
        late = await live.follow_up("too late", command_id="late", wait=1)
        return receipt.outcome, outcome.agent_text, late.code

    receipt_outcome, agent_text, late_code = asyncio.run(run())

    assert receipt_outcome is ControlOutcome.ACCEPTED_NEW_TURN
    assert agent_text.endswith("second turn")
    assert len(session.prompts) == 1
    assert late_code is RejectCode.SESSION_CLOSED


def test_a_session_without_follow_up_closes_after_its_first_result() -> None:
    capture = StreamCapture()
    capture.observe(result_message())
    live = _controller(capture)

    session = cast("HarnessSession", object())
    started = monotonic()
    outcome = asyncio.run(hold_idle_window(session, capture, live, capture.outcome(), budget=IdleWindowBudget(1, None)))

    assert not can_idle(session, outcome, live)
    assert monotonic() - started < 0.1
    assert live.state == "closed"


@pytest.mark.parametrize("failure", ["raise", "timeout"])
def test_a_failed_follow_up_start_is_unknown_and_is_not_retried(failure: str) -> None:
    class FailingSession(_CapableSession):
        def __init__(self) -> None:
            self.starts = 0

        async def follow_up(self, prompt: str) -> None:
            self.starts += 1
            if failure == "raise":
                message = "transport failed"
                raise RuntimeError(message)
            await asyncio.Event().wait()

    session = FailingSession()
    capture = StreamCapture()
    capture.observe(result_message())
    live = _controller(capture)
    live.bind(session)

    async def run() -> tuple[ControlOutcome, RejectCode | None]:
        held = asyncio.create_task(
            hold_idle_window(session, capture, live, capture.outcome(), budget=IdleWindowBudget(0.1, None))
        )
        await asyncio.sleep(0)
        assert live.idle
        receipt = await live.follow_up("again", command_id="one", wait=0.001)
        await held
        late = await live.follow_up("again", command_id="two", wait=0.001)
        return receipt.outcome, late.code

    outcome, late_code = asyncio.run(run())
    assert outcome is ControlOutcome.UNKNOWN_DELIVERY
    assert late_code is RejectCode.SESSION_CLOSED
    assert session.starts == 1
