"""A command whose delivery the owner loop never confirmed stays ``unknown_delivery`` and is never replayed."""

import asyncio
from dataclasses import replace
from unittest.mock import patch

from teatree.agents import live_registry
from teatree.agents.live_control import ControlOutcome, ControlReceipt, LiveFence, LiveTask, RejectCode
from teatree.agents.live_registry import LiveSessionRegistry
from tests.teatree_agents._live_owner import OwnerLoop

_TASK = LiveTask(pk=7, ticket=3, phase="coding", harness="claude_sdk", model="m")


class _SilentSession:
    """Takes a steer and never says whether the running turn accepted it."""

    def __init__(self) -> None:
        self.steered: list[str] = []

    async def steer(self, text: str, *, input_id: str, wait: float) -> None:
        del text, wait
        self.steered.append(input_id)
        await asyncio.Event().wait()

    async def interrupt(self) -> None:
        return None


class _FailingSession(_SilentSession):
    """Takes a steer, then fails without saying whether the running turn already holds it."""

    async def steer(self, text: str, *, input_id: str, wait: float) -> None:
        del text, wait
        self.steered.append(input_id)
        msg = "adapter lost the session mid-delivery"
        raise RuntimeError(msg)


def _steer(registry: LiveSessionRegistry) -> ControlReceipt:
    return asyncio.run(
        registry.steer(_TASK.pk, "use the attached page", command_id="c-1", wait=0.05, fence=_TASK.fence)
    )


def test_an_unconfirmed_command_stays_unknown_for_every_retry_and_is_delivered_once() -> None:
    registry = LiveSessionRegistry()
    session = _SilentSession()
    owner = OwnerLoop(registry, _TASK, session)
    owner.start()
    assert owner.attached.wait(timeout=10)

    with patch.object(live_registry, "_OWNER_LOOP_GRACE_SECONDS", 0.05):
        first = _steer(registry)
        retried_while_live = _steer(registry)
        owner.end()
        retried_after_close = _steer(registry)

    outcomes = [first.outcome, retried_while_live.outcome, retried_after_close.outcome]
    assert outcomes == [ControlOutcome.UNKNOWN_DELIVERY] * 3
    assert retried_after_close.code is None
    assert session.steered == ["c-1"]


def test_a_delivery_that_fails_mid_flight_stays_unknown_for_every_retry_and_is_delivered_once() -> None:
    registry = LiveSessionRegistry()
    session = _FailingSession()
    owner = OwnerLoop(registry, _TASK, session)
    owner.start()
    assert owner.attached.wait(timeout=10)

    first = _steer(registry)
    retried_while_live = _steer(registry)
    owner.end()
    retried_after_close = _steer(registry)

    outcomes = [first.outcome, retried_while_live.outcome, retried_after_close.outcome]
    assert outcomes == [ControlOutcome.UNKNOWN_DELIVERY] * 3
    assert session.steered == ["c-1"]


class _AcceptingSession(_SilentSession):
    async def steer(self, text: str, *, input_id: str, wait: float) -> None:
        del text, wait
        self.steered.append(input_id)


def _push(registry: LiveSessionRegistry) -> ControlReceipt:
    return asyncio.run(registry.push(_TASK.pk, "framed peer words", command_id="peer-1", wait=1, fence=_TASK.fence))


def test_a_peer_push_runs_on_the_owner_loop_and_keeps_no_receipt_after_close() -> None:
    registry = LiveSessionRegistry()
    session = _AcceptingSession()
    owner = OwnerLoop(registry, _TASK, session)
    owner.start()
    assert owner.attached.wait(timeout=10)

    pushed = _push(registry)
    owner.end()
    after_close = _push(registry)

    assert pushed.outcome is ControlOutcome.ACCEPTED_CURRENT_TURN
    assert (after_close.outcome, after_close.code) == (ControlOutcome.REJECTED, RejectCode.SESSION_CLOSED)
    assert session.steered == ["peer-1"]


def test_a_peer_push_to_a_task_with_no_live_session_is_unknown_session() -> None:
    receipt = _push(LiveSessionRegistry())

    assert (receipt.outcome, receipt.code) == (ControlOutcome.REJECTED, RejectCode.UNKNOWN_SESSION)


def test_a_command_for_an_older_attempt_never_reaches_the_newer_one() -> None:
    registry = LiveSessionRegistry()
    old = replace(_TASK, fence=LiveFence("old", "generation"))
    new = replace(_TASK, fence=LiveFence("new", "generation"))
    with OwnerLoop(registry, old, _AcceptingSession()):
        pass
    session = _AcceptingSession()
    with OwnerLoop(registry, new, session):
        stale = asyncio.run(registry.steer(new.pk, "old words", command_id="same", wait=1, fence=old.fence))
        stale_push = asyncio.run(registry.push(new.pk, "old peer", command_id="old-peer", wait=1, fence=old.fence))
        current = asyncio.run(registry.steer(new.pk, "new words", command_id="same", wait=1, fence=new.fence))
    assert (stale.outcome, stale.code) == (ControlOutcome.REJECTED, RejectCode.STALE_COMMAND)
    assert (stale_push.outcome, stale_push.code) == (ControlOutcome.REJECTED, RejectCode.STALE_COMMAND)
    assert current.outcome is ControlOutcome.ACCEPTED_CURRENT_TURN
    assert session.steered == ["same"]


def test_a_command_for_another_generation_is_stale() -> None:
    registry = LiveSessionRegistry()
    current = replace(_TASK, fence=LiveFence("attempt", "new-generation"))
    session = _AcceptingSession()
    with OwnerLoop(registry, current, session):
        receipt = asyncio.run(
            registry.steer(
                current.pk, "old generation", command_id="g-1", wait=1, fence=LiveFence("attempt", "old-generation")
            )
        )
    assert (receipt.outcome, receipt.code) == (ControlOutcome.REJECTED, RejectCode.STALE_COMMAND)
    assert session.steered == []


def test_a_closed_tombstone_rejects_another_fence_before_replaying_a_receipt() -> None:
    registry = LiveSessionRegistry()
    with OwnerLoop(registry, _TASK, _AcceptingSession()):
        original = _steer(registry)

    stale = asyncio.run(
        registry.steer(
            _TASK.pk,
            "use the attached page",
            command_id="c-1",
            wait=0.05,
            fence=LiveFence("old-attempt", _TASK.fence.generation),
        )
    )

    assert original.outcome is ControlOutcome.ACCEPTED_CURRENT_TURN
    assert (stale.outcome, stale.code) == (ControlOutcome.REJECTED, RejectCode.STALE_COMMAND)


def test_a_reused_command_id_cannot_replay_into_a_new_attempt() -> None:
    registry = LiveSessionRegistry()
    old = replace(_TASK, fence=LiveFence("old", "g"))
    new = replace(_TASK, fence=LiveFence("new", "g"))
    with OwnerLoop(registry, old, _AcceptingSession()):
        original = asyncio.run(registry.steer(old.pk, "same words", command_id="reused", wait=1, fence=old.fence))
    session = _AcceptingSession()
    with OwnerLoop(registry, new, session):
        retried = asyncio.run(registry.steer(new.pk, "same words", command_id="reused", wait=1, fence=old.fence))
    assert original.outcome is ControlOutcome.ACCEPTED_CURRENT_TURN
    assert (retried.outcome, retried.code) == (ControlOutcome.REJECTED, RejectCode.STALE_COMMAND)
    assert session.steered == []
