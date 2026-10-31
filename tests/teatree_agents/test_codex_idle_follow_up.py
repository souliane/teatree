"""A finished Codex turn holds its thread open and takes one follow-up turn through the live controller."""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from teatree.agents.live_control import ControlOutcome, ControlReceipt, LiveSessionController, LiveTask
from teatree.agents.live_idle import IdleWindowBudget, hold_idle_window
from teatree.agents.runner_stream import StreamCapture, drain_turn
from tests.teatree_agents.test_codex_shared_app_server_credential import _ONE, _RECORDING_SERVER, _serving, _World


@pytest.fixture
def world(tmp_path: Path) -> Iterator[_World]:
    yield from _serving(tmp_path, _RECORDING_SERVER)


def test_an_idle_codex_follow_up_is_accepted_as_a_new_turn_once_on_the_same_thread(world: _World) -> None:
    async def run() -> tuple[ControlReceipt, ControlReceipt]:
        session = await world.open_session(_ONE)
        capture = StreamCapture()
        live = LiveSessionController(LiveTask(7, 3, "coding", "codex_app_server", "codex"), capture)
        live.bind(session)
        await session.query("first")
        first_outcome = await drain_turn(session, capture)
        held = asyncio.create_task(
            hold_idle_window(session, capture, live, first_outcome, budget=IdleWindowBudget(10.0, None))
        )
        await asyncio.sleep(0)
        assert live.idle
        receipt = await live.follow_up("nonce", command_id="slack:1", wait=10)
        replay = await live.follow_up("nonce", command_id="slack:1", wait=10)
        live.close()
        await held
        await session.close()
        return receipt, replay

    receipt, replay = asyncio.run(run())

    assert receipt.outcome is ControlOutcome.ACCEPTED_NEW_TURN
    assert replay == receipt
    assert world.turn_starts() == ["thread-1", "thread-1"]
