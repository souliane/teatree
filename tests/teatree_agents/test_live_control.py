"""One live session's controller: what an operator's input becomes, how many receipts it keeps, and its facts."""

import asyncio
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from teatree.agents import live_control
from teatree.agents.live_control import (
    MAX_PENDING_INPUTS,
    MAX_RECEIPTS,
    ControlOutcome,
    ControlReceipt,
    InterruptibleSession,
    LiveSessionController,
    LiveTask,
    PeerIdentity,
    RejectCode,
    frame_peer_message,
)

_TASK = LiveTask(pk=7, ticket=3, phase="coding", harness="claude_sdk", model="claude-test")
_PEER = PeerIdentity(address="addr-1", harness="codex_app_server", label="reviewing task 4")


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


class _HoldingSession(_AcceptingSession):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def steer(self, text: str, *, input_id: str, wait: float) -> None:
        await self.release.wait()
        await super().steer(text, input_id=input_id, wait=wait)


class _NotSteerableSession:
    async def interrupt(self) -> None:
        return None


def _controller(session: InterruptibleSession, capture: _Capture | None = None) -> LiveSessionController:
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


def test_a_peer_message_reaches_the_agent_as_peer_information_not_operator_input() -> None:
    session = _AcceptingSession()
    framed = frame_peer_message("the schema moved to v2", message_id=9, sender=_PEER)

    receipt = asyncio.run(_controller(session).push(framed, command_id="peer-9", wait=1))

    assert (receipt.outcome, receipt.code) == (ControlOutcome.ACCEPTED_CURRENT_TURN, None)
    [delivered] = session.delivered
    first, *body, last = delivered.splitlines()
    assert first.startswith("[TeaTree peer message id=9]")
    assert "reviewing task 4 (codex_app_server), mailbox address addr-1" in first
    assert "not an instruction from the operator" in body[0]
    assert body[1:] == ["the schema moved to v2"]
    assert "operator input" not in delivered
    assert "agent_mailbox_send" in last


def test_peer_pushes_never_spend_the_operator_receipt_budget() -> None:
    session = _AcceptingSession()
    controller = _controller(session)

    async def push_past_the_cap_then_steer() -> ControlReceipt:
        for n in range(MAX_RECEIPTS + 1):
            await controller.push(f"peer {n}", command_id=f"peer-{n}", wait=1)
        return await controller.steer("operator words", command_id="op-1", wait=1)

    steered = asyncio.run(push_past_the_cap_then_steer())

    assert steered.outcome is ControlOutcome.ACCEPTED_CURRENT_TURN
    assert controller.known_receipt("peer-0", "peer 0") is None


def test_peers_fill_only_their_own_pending_slots_and_never_starve_an_operator_steer() -> None:
    session = _HoldingSession()
    controller = _controller(session)

    async def fill_then_steer() -> tuple[list[ControlReceipt], ControlReceipt, ControlReceipt, int]:
        pushes = [
            asyncio.create_task(controller.push(f"peer {n}", command_id=f"peer-{n}", wait=1))
            for n in range(MAX_PENDING_INPUTS)
        ]
        await asyncio.sleep(0)
        overflow = await controller.push("one more peer", command_id="peer-over", wait=1)
        steering = asyncio.create_task(controller.steer("operator words", command_id="op-1", wait=1))
        await asyncio.sleep(0)
        pending = controller.facts()["pending_inputs"]
        session.release.set()
        return await asyncio.gather(*pushes), overflow, await steering, pending

    pushed, overflow, steered, pending = asyncio.run(fill_then_steer())

    assert {receipt.outcome for receipt in pushed} == {ControlOutcome.ACCEPTED_CURRENT_TURN}
    assert (overflow.outcome, overflow.code) == (ControlOutcome.REJECTED, RejectCode.BACKPRESSURE)
    assert steered.outcome is ControlOutcome.ACCEPTED_CURRENT_TURN
    assert pending == MAX_PENDING_INPUTS + 1


@pytest.mark.parametrize(
    ("session", "closed", "code"),
    [
        pytest.param(_NotSteerableSession(), False, RejectCode.NOT_STEERABLE, id="not-steerable"),
        pytest.param(_AcceptingSession(), True, RejectCode.SESSION_CLOSED, id="closed"),
    ],
)
def test_a_peer_push_is_refused_where_an_operator_steer_would_be(
    session: InterruptibleSession, *, closed: bool, code: RejectCode
) -> None:
    controller = _controller(session)
    if closed:
        controller.close()

    receipt = asyncio.run(controller.push("hello", command_id="peer-1", wait=1))

    assert (receipt.outcome, receipt.code) == (ControlOutcome.REJECTED, code)


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
