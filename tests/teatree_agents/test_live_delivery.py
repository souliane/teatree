"""A peer message's receipt: the first verdict wins, and settling withdraws a push still in flight."""

import asyncio

from teatree.agents.live_control import ControlOutcome, RejectCode
from teatree.agents.live_delivery import PeerDelivery, SendReceipt


def _delivery() -> PeerDelivery:
    return PeerDelivery(5, "addr-2", "hello", "[framed] hello", asyncio.get_running_loop().create_future())


async def _never_returns() -> None:
    await asyncio.Event().wait()


def test_an_unsettled_message_reads_as_not_accepted_in_time_and_still_queued() -> None:
    async def unsettled() -> SendReceipt:
        await asyncio.sleep(0)
        return _delivery().receipt()

    assert asyncio.run(unsettled()) == {
        "id": 5,
        "to": "addr-2",
        "outcome": "rejected",
        "code": "not_accepted_in_time",
        "queued_in_inbox": True,
    }


def test_the_first_verdict_wins_over_a_later_one() -> None:
    async def settle_twice() -> SendReceipt:
        await asyncio.sleep(0)
        delivery = _delivery()
        delivery.settle(ControlOutcome.ACCEPTED_CURRENT_TURN)
        delivery.settle(ControlOutcome.REJECTED, RejectCode.SESSION_CLOSED)
        return delivery.receipt()

    receipt = asyncio.run(settle_twice())

    assert (receipt["outcome"], receipt["code"], receipt["queued_in_inbox"]) == ("accepted_current_turn", None, False)


def test_settling_withdraws_a_push_still_waiting_for_its_boundary() -> None:
    async def settle_during_push() -> tuple[bool, SendReceipt]:
        delivery = _delivery()
        delivery.push = asyncio.create_task(_never_returns())
        await asyncio.sleep(0)
        delivery.settle(ControlOutcome.ACCEPTED_INBOX_READ)
        await asyncio.wait({delivery.push})
        return delivery.push.cancelled(), delivery.receipt()

    withdrawn, receipt = asyncio.run(settle_during_push())

    assert withdrawn is True
    assert receipt["outcome"] == "accepted_inbox_read"
