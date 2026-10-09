"""One live session's controller: what an operator's input becomes, how many receipts it keeps, and its facts."""

import asyncio
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from teatree.agents import live_control
from teatree.agents.live_control import (
    MAX_RECEIPTS,
    ControlOutcome,
    ControlReceipt,
    LiveSessionController,
    LiveTask,
    RejectCode,
)

_TASK = LiveTask(pk=7, ticket=3, phase="coding", harness="claude_sdk", model="claude-test")


@dataclass
class _Capture:
    tool_calls: int = 0
    context_tokens: int | None = None
    last_event_at: float | None = None
    open_tool: tuple[str, float] | None = None
    result_message: object = None


class _AcceptingSession:
    def __init__(self) -> None:
        self.delivered: list[str] = []

    async def steer(self, text: str, *, input_id: str, wait: float) -> None:
        del input_id, wait
        self.delivered.append(text)

    async def interrupt(self) -> None:
        return None


def _controller(session: _AcceptingSession, capture: _Capture | None = None) -> LiveSessionController:
    controller = LiveSessionController(_TASK, capture or _Capture())
    controller.bind(session)
    return controller


def test_the_input_reaches_the_agent_named_as_operator_input_that_keeps_its_envelope() -> None:
    session = _AcceptingSession()

    asyncio.run(_controller(session).steer("use docs/x.md", command_id="c-1", wait=1))

    [delivered] = session.delivered
    first, *body, last = delivered.splitlines()
    assert first.startswith("[TeaTree operator input id=c-1]")
    assert body == ["use docs/x.md"]
    assert "result envelope" in last


def test_past_the_receipt_cap_a_new_command_is_backpressure_and_a_kept_one_still_replays() -> None:
    session = _AcceptingSession()
    controller = _controller(session)

    async def fill_then_overflow() -> tuple[ControlReceipt, ControlReceipt]:
        for n in range(MAX_RECEIPTS):
            await controller.steer(f"input {n}", command_id=f"c-{n}", wait=1)
        overflow = await controller.steer("one more", command_id="c-over", wait=1)
        replay = await controller.steer("input 0", command_id="c-0", wait=1)
        return overflow, replay

    overflow, replay = asyncio.run(fill_then_overflow())

    assert (overflow.outcome, overflow.code) == (ControlOutcome.REJECTED, RejectCode.BACKPRESSURE)
    assert replay.outcome is ControlOutcome.ACCEPTED_CURRENT_TURN
    assert len(session.delivered) == MAX_RECEIPTS


@pytest.mark.parametrize(
    ("capture", "closed", "state", "steerable"),
    [
        pytest.param(_Capture(), False, "starting", True, id="no-event-yet"),
        pytest.param(_Capture(last_event_at=111.0, result_message=object()), False, "finishing", True, id="result-in"),
        pytest.param(_Capture(last_event_at=111.0), True, "closed", False, id="closed"),
    ],
)
def test_the_state_follows_the_stream_and_a_closed_session_is_not_steerable(
    capture: _Capture, *, closed: bool, state: str, steerable: bool
) -> None:
    controller = _controller(_AcceptingSession(), capture)
    if closed:
        controller.close()

    facts = controller.facts()

    assert (facts["state"], facts["steerable"]) == (state, steerable)


def test_the_facts_age_every_clock_reading_from_one_now() -> None:
    capture = _Capture(tool_calls=4, context_tokens=52_000, last_event_at=111.0, open_tool=("Bash", 110.0))
    with patch.object(live_control, "monotonic", side_effect=[100.0]):
        controller = _controller(_AcceptingSession(), capture)
    with patch.object(live_control, "monotonic", side_effect=[112.5]):
        facts = controller.facts()

    assert facts == {
        "task": 7,
        "ticket": 3,
        "phase": "coding",
        "harness": "claude_sdk",
        "model": "claude-test",
        "state": "busy",
        "steerable": True,
        "pending_inputs": 0,
        "elapsed_seconds": 12.5,
        "tool_calls": 4,
        "open_tool": "Bash",
        "open_tool_seconds": 2.5,
        "last_event_age_seconds": 1.5,
        "context_tokens": 52_000,
    }
