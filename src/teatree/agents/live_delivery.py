"""Live mailbox delivery and participant state."""

import asyncio
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn, Protocol, TypedDict

from teatree.agents.live_control import (
    MAX_INPUT_BYTES,
    ControlOutcome,
    ControlReceipt,
    LiveTask,
    PeerIdentity,
    RejectCode,
)

MAX_WAIT_SECONDS = 20
MAX_KEY_BYTES = 128
MAX_SENT_KEYS = 1_000
_MAX_INBOX_MESSAGES = 1_000
MAILBOX_SOCKET_ENV = "T3_AGENT_MAILBOX_SOCKET"
MAILBOX_AUTH_ENV = "T3_AGENT_MAILBOX_TOKEN"
MAILBOX_PHASE_ENV = "T3_AGENT_MAILBOX_PHASE"
TICKET_ID_ENV = "T3_TICKET_ID"


def _redacted_token_repr(_value: str) -> str:
    return "'<redacted>'"


MailboxToken = type("MailboxToken", (str,), {"__slots__": (), "__repr__": _redacted_token_repr})


type MailboxRequest = Mapping[str, object]


class MailboxTextTooLargeError(ValueError):
    def __init__(self) -> None:
        super().__init__("Mailbox text must be 1-16384 bytes")


InboxMessage = TypedDict(
    "InboxMessage",
    {"id": int, "text": str, "from": str, "from_harness": str, "from_label": str},
)


class InboxPage(TypedDict):
    messages: list[InboxMessage]
    next_cursor: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class MailboxIdentity:
    socket_path: Path
    address: str
    token: str = field(repr=False)

    def mcp_env(self) -> dict[str, str]:
        return {MAILBOX_SOCKET_ENV: str(self.socket_path), MAILBOX_AUTH_ENV: MailboxToken(self.token)}


class PeerDescriptor(Protocol):
    address: str
    harness: str
    label: str
    can_receive: bool
    room: str
    phase: str


def peer_identity(peer: PeerDescriptor) -> PeerIdentity:
    identity: PeerIdentity = {
        "address": peer.address,
        "harness": peer.harness,
        "label": peer.label,
        "can_receive": peer.can_receive,
    }
    if peer.room:
        identity["ticket"] = peer.room
        identity["room"] = peer.room
    if peer.phase:
        identity["phase"] = peer.phase
    return identity


def send_fields(request: MailboxRequest) -> tuple[str, str, str, float]:
    to, body, key = request.get("to"), request.get("text"), request.get("key")
    timeout = request.get("timeout_seconds", MAX_WAIT_SECONDS)
    if not isinstance(to, str) or not isinstance(body, str) or not isinstance(key, str):
        _reject("Mailbox send requires to, text, and key strings")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= MAX_WAIT_SECONDS:
        _reject("Mailbox acceptance timeout must be between 0 and 20 seconds")
    if not key or len(key.encode()) > MAX_KEY_BYTES:
        _reject("Mailbox key must be 1-128 bytes")
    if not body or len(body.encode()) > MAX_INPUT_BYTES:
        if body:
            raise MailboxTextTooLargeError
        _reject("Mailbox text must be 1-16384 bytes")
    return to, body, key, float(timeout)


def _reject(message: str) -> NoReturn:
    raise ValueError(message)


class SendReceipt(TypedDict):
    id: int
    to: str
    outcome: str
    code: str | None
    queued_in_inbox: bool


def refused_receipt(to: str, code: RejectCode) -> SendReceipt:
    return {"id": 0, "to": to, "outcome": ControlOutcome.REJECTED.value, "code": code.value, "queued_in_inbox": False}


@dataclass(slots=True)
class RemoteReceipts:
    records: dict[str, tuple[str, str, SendReceipt]] = field(default_factory=dict)

    def prior(self, key: str, to: str, text: str) -> SendReceipt | None:
        record = self.records.get(key)
        if record is None:
            return None
        if record[:2] != (to, text):
            _reject("Mailbox key already used for another message")
        return None if record[2]["code"] == RejectCode.NOT_ACCEPTED_IN_TIME.value else record[2]

    def keep(self, key: str, to: str, text: str, receipt: SendReceipt) -> None:
        record = self.records.get(key)
        if record is None or record[2]["code"] == RejectCode.NOT_ACCEPTED_IN_TIME.value:
            self.records[key] = (to, text, receipt)

    def is_full_for(self, key: str, local_count: int) -> bool:
        return key not in self.records and local_count + len(self.records) >= MAX_SENT_KEYS


@dataclass(slots=True)
class PeerDelivery:
    message_id: int
    to: str
    text: str
    framed: str
    settled: asyncio.Future[tuple[ControlOutcome, RejectCode | None]]
    push: asyncio.Task[None] | None = None
    push_outcome: ControlOutcome | None = None
    unknown_reported: bool = False
    queued_in_inbox: bool = True

    def record_push(self, receipt: ControlReceipt) -> None:
        self.push_outcome = receipt.outcome
        if receipt.outcome is ControlOutcome.UNKNOWN_DELIVERY:
            return
        if (
            receipt.outcome is ControlOutcome.REJECTED
            and receipt.code is RejectCode.NOT_ACCEPTED_IN_TIME
            and self.queued_in_inbox
        ):
            return
        self.settle(receipt.outcome, receipt.code)

    def settle(self, outcome: ControlOutcome, code: RejectCode | None = None) -> None:
        """Keep the first verdict; a push still waiting for the turn's boundary is withdrawn."""
        if self.settled.done():
            return
        if outcome is ControlOutcome.ACCEPTED_INBOX_READ:
            self.queued_in_inbox = False
        self.settled.set_result((outcome, code))
        if self.push is not None and self.push is not asyncio.current_task():
            self.push.cancel()

    def receipt(self) -> SendReceipt:
        if not self.settled.done():
            if self.unknown_reported:
                return self.as_receipt(ControlOutcome.UNKNOWN_DELIVERY, None)
            return self.as_receipt(ControlOutcome.REJECTED, RejectCode.NOT_ACCEPTED_IN_TIME)
        outcome, code = self.settled.result()
        return self.as_receipt(outcome, code)

    def as_receipt(self, outcome: ControlOutcome, code: RejectCode | None) -> SendReceipt:
        return {
            "id": self.message_id,
            "to": self.to,
            "outcome": outcome.value,
            "code": code.value if code else None,
            "queued_in_inbox": self.queued_in_inbox,
        }


@dataclass(slots=True)
class _Participant:
    address: str
    token: str = field(repr=False)
    room: str
    harness: str
    label: str
    phase: str = ""
    can_receive: bool = True
    task: LiveTask | None = None
    reads_inbox: bool = True
    inbox: deque[InboxMessage] = field(default_factory=lambda: deque(maxlen=_MAX_INBOX_MESSAGES))
    sent: dict[str, PeerDelivery] = field(default_factory=dict)
    remote_sent: RemoteReceipts = field(default_factory=RemoteReceipts)
    dropped_before: int = 0
    waiting: int = 0


@dataclass(frozen=True, slots=True)
class PushOnlyTask:
    task: LiveTask
