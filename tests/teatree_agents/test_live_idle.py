"""Only a clean capable session enters the bounded idle window."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from time import monotonic
from typing import cast

import pytest
from claude_agent_sdk import ResultMessage

from teatree.agents.claude_sdk_session import ClaudeSdkSession
from teatree.agents.codex_app_server import CodexAppServerSession
from teatree.agents.codex_shared_app_server import SharedCodexSession
from teatree.agents.harness import HarnessSession, PydanticAiHarnessSession
from teatree.agents.live_control import ControlOutcome, FollowUpSession, LiveSessionController, LiveTask, RejectCode
from teatree.agents.live_idle import IdleWindowBudget, can_idle, hold_idle_window
from teatree.agents.runner_stream import StreamCapture
from teatree.core.models.task_attempt import RESUME_CONTEXT_CAP_TOKENS
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
    capture.observe(result_message(is_error=False))
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


def test_close_during_follow_up_start_does_not_crash_the_driver() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowStart(_CapableSession):
        async def follow_up(self, prompt: str) -> None:
            entered.set()
            await release.wait()

        async def receive_response(self) -> AsyncIterator[object]:
            yield result_message(result="second")

    async def run() -> ControlOutcome:
        capture = StreamCapture()
        capture.observe(result_message(result="first"))
        session = SlowStart()
        live = _controller(capture)
        live.bind(session)
        held = asyncio.create_task(
            hold_idle_window(session, capture, live, capture.outcome(), budget=IdleWindowBudget(1, None))
        )
        await asyncio.sleep(0)
        requested = asyncio.create_task(live.follow_up("next", command_id="one", wait=1))
        await entered.wait()
        live.close()
        release.set()
        await held
        return (await requested).outcome

    assert asyncio.run(run()) is ControlOutcome.UNKNOWN_DELIVERY


def test_a_failed_follow_up_drain_is_recorded_as_failed() -> None:
    class BrokenDrain(_CapableSession):
        async def receive_response(self) -> AsyncIterator[object]:
            message = "broken pipe"
            raise RuntimeError(message)
            yield result_message()

    async def run() -> str | None:
        capture = StreamCapture()
        capture.observe(result_message(result="first"))
        session = BrokenDrain()
        live = _controller(capture)
        live.bind(session)
        held = asyncio.create_task(
            hold_idle_window(session, capture, live, capture.outcome(), budget=IdleWindowBudget(0.1, None))
        )
        await asyncio.sleep(0)
        receipt = await live.follow_up("next", command_id="one", wait=1)
        assert receipt.outcome is ControlOutcome.ACCEPTED_NEW_TURN
        return (await held).stuck_reason

    assert asyncio.run(run()) is not None


@pytest.mark.parametrize("failure", ["exception", "no_result"])
def test_failed_follow_up_retains_the_first_result_metadata(failure: str) -> None:
    class FailedTurn(_CapableSession):
        async def receive_response(self) -> AsyncIterator[object]:
            yield assistant_text("partial second turn")
            if failure == "exception":
                message = "broken stream"
                raise RuntimeError(message)

    async def run() -> tuple[ResultMessage | None, str | None]:
        capture = StreamCapture()
        first = result_message(result="first", num_turns=3, total_cost_usd=1.25, usage={"input_tokens": 100})
        capture.observe(first)
        session = FailedTurn()
        live = _controller(capture)
        live.bind(session)
        held = asyncio.create_task(
            hold_idle_window(session, capture, live, capture.outcome(), budget=IdleWindowBudget(0.1, None))
        )
        await asyncio.sleep(0)
        receipt = await live.follow_up("next", command_id="one", wait=1)
        assert receipt.outcome is ControlOutcome.ACCEPTED_NEW_TURN
        outcome = await held
        return outcome.result_message, outcome.stuck_reason

    result, reason = asyncio.run(run())
    assert reason is not None
    assert result is not None
    assert result.num_turns == 3
    assert result.total_cost_usd == pytest.approx(1.25)
    assert result.usage == {"input_tokens": 100}


def test_compaction_stop_in_a_follow_up_prevents_another_idle_window() -> None:
    stopped = False

    class CompactedTurn(_CapableSession):
        async def receive_response(self) -> AsyncIterator[object]:
            nonlocal stopped
            stopped = True
            yield result_message(result="second")

    async def run() -> tuple[bool, str]:
        capture = StreamCapture()
        capture.observe(result_message(result="first"))
        session = CompactedTurn()
        live = _controller(capture)
        live.bind(session)
        held = asyncio.create_task(
            hold_idle_window(
                session,
                capture,
                live,
                capture.outcome(),
                budget=IdleWindowBudget(1, None, lambda: stopped),
            )
        )
        await asyncio.sleep(0)
        receipt = await live.follow_up("next", command_id="one", wait=1)
        assert receipt.outcome is ControlOutcome.ACCEPTED_NEW_TURN
        outcome = await held
        return outcome.compaction_stopped, live.state

    assert asyncio.run(run()) == (True, "closed")


def test_a_streaming_follow_up_is_busy_and_cannot_reuse_the_first_result() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class ResultlessTurn(_CapableSession):
        async def receive_response(self) -> AsyncIterator[object]:
            entered.set()
            await release.wait()
            yield assistant_text("partial")

    async def run() -> tuple[str, object, str | None]:
        capture = StreamCapture()
        capture.observe(result_message(result="first"))
        session = ResultlessTurn()
        live = _controller(capture)
        live.bind(session)
        held = asyncio.create_task(
            hold_idle_window(session, capture, live, capture.outcome(), budget=IdleWindowBudget(0.1, None))
        )
        await asyncio.sleep(0)
        receipt = await live.follow_up("next", command_id="one", wait=1)
        assert receipt.outcome is ControlOutcome.ACCEPTED_NEW_TURN
        await entered.wait()
        state, previous_result = live.state, capture.result_message
        release.set()
        outcome = await held
        return state, previous_result, outcome.stuck_reason

    state, previous_result, failure = asyncio.run(run())
    assert state == "busy"
    assert previous_result is None
    assert failure == "follow-up turn ended without a result"


class _TurnSession(_CapableSession):
    def __init__(self, capture: StreamCapture, *, context_after_turn: int | None = None) -> None:
        self.capture = capture
        self.context_after_turn = context_after_turn
        self.prompts: list[str] = []

    async def follow_up(self, prompt: str) -> None:
        self.prompts.append(prompt)

    async def receive_response(self) -> AsyncIterator[object]:
        if self.context_after_turn is not None:
            self.capture.context_tokens = self.context_after_turn
        yield assistant_text("next turn")
        yield result_message(result="next turn")


def _follow_up_after_first_result(
    context_tokens: int, *, context_after_turn: int | None = None, follow_ups: int = 1
) -> tuple[list[RejectCode | ControlOutcome], int]:
    async def run() -> tuple[list[RejectCode | ControlOutcome], int]:
        capture = StreamCapture()
        capture.observe(result_message(result="first"))
        capture.context_tokens = context_tokens
        session = _TurnSession(capture, context_after_turn=context_after_turn)
        live = _controller(capture)
        live.bind(session)
        held = asyncio.create_task(
            hold_idle_window(
                cast("HarnessSession", session), capture, live, capture.outcome(), budget=IdleWindowBudget(1, None)
            )
        )
        verdicts: list[RejectCode | ControlOutcome] = []
        for index in range(follow_ups):
            await asyncio.sleep(0.05)
            receipt = await live.follow_up(f"turn {index}", command_id=f"c{index}", wait=1)
            verdicts.append(receipt.code or receipt.outcome)
        await held
        return verdicts, len(session.prompts)

    return asyncio.run(run())


def test_a_conversation_over_the_resume_cap_opens_no_idle_window() -> None:
    verdicts, prompts_sent = _follow_up_after_first_result(RESUME_CONTEXT_CAP_TOKENS + 1)

    assert verdicts == [RejectCode.SESSION_CLOSED]
    assert prompts_sent == 0


def test_a_conversation_exactly_at_the_resume_cap_still_opens_the_window() -> None:
    verdicts, prompts_sent = _follow_up_after_first_result(RESUME_CONTEXT_CAP_TOKENS)

    assert verdicts == [ControlOutcome.ACCEPTED_NEW_TURN]
    assert prompts_sent == 1


@pytest.mark.parametrize(
    ("context_tokens", "verdicts", "prompts_sent"),
    [
        (200_000, [ControlOutcome.ACCEPTED_NEW_TURN], 1),
        (200_001, [RejectCode.SESSION_CLOSED], 0),
    ],
    ids=["two-hundred-thousand-opens-the-window", "one-over-two-hundred-thousand-opens-none"],
)
def test_the_idle_window_closes_above_two_hundred_thousand_tokens(
    context_tokens: int, verdicts: list[RejectCode | ControlOutcome], prompts_sent: int
) -> None:
    assert _follow_up_after_first_result(context_tokens) == (verdicts, prompts_sent)


def test_a_follow_up_turn_that_crosses_the_cap_finishes_and_the_next_window_is_refused() -> None:
    verdicts, prompts_sent = _follow_up_after_first_result(
        100_000, context_after_turn=RESUME_CONTEXT_CAP_TOKENS + 1, follow_ups=2
    )

    assert verdicts == [ControlOutcome.ACCEPTED_NEW_TURN, RejectCode.SESSION_CLOSED]
    assert prompts_sent == 1
