"""One peer message on its way to a live session: the first acceptance settles it and becomes its receipt."""

import asyncio
from dataclasses import dataclass
from typing import TypedDict

from teatree.agents.live_control import ControlOutcome, RejectCode


class SendReceipt(TypedDict):
    id: int
    to: str
    outcome: str
    code: str | None
    queued_in_inbox: bool


@dataclass(slots=True)
class PeerDelivery:
    message_id: int
    to: str
    text: str
    framed: str
    settled: asyncio.Future[tuple[ControlOutcome, RejectCode | None]]
    push: asyncio.Task[None] | None = None

    def settle(self, outcome: ControlOutcome, code: RejectCode | None = None) -> None:
        """Keep the first verdict; a push still waiting for the turn's boundary is withdrawn."""
        if self.settled.done():
            return
        self.settled.set_result((outcome, code))
        if self.push is not None:
            self.push.cancel()

    def receipt(self) -> SendReceipt:
        if not self.settled.done():
            return self._as_receipt(ControlOutcome.REJECTED, RejectCode.NOT_ACCEPTED_IN_TIME, queued_in_inbox=True)
        outcome, code = self.settled.result()
        return self._as_receipt(outcome, code, queued_in_inbox=False)

    def _as_receipt(self, outcome: ControlOutcome, code: RejectCode | None, *, queued_in_inbox: bool) -> SendReceipt:
        return {
            "id": self.message_id,
            "to": self.to,
            "outcome": outcome.value,
            "code": code.value if code else None,
            "queued_in_inbox": queued_in_inbox,
        }
