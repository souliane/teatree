"""Harness-neutral mid-run control of one live session: typed outcomes, receipts and the controller.

A receipt records SESSION ACCEPTANCE — the harness took the input into the running turn — never
model obedience. The only per-harness surface is :class:`SteerableSession`.
"""

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from typing import Protocol, TypedDict, runtime_checkable

MAX_INPUT_BYTES = 16_384
MAX_PENDING_INPUTS = 4
MAX_RECEIPTS = 256
DEFAULT_STEER_WAIT_SECONDS = 60.0
MAX_STEER_WAIT_SECONDS = 900.0
_DRAIN_SECONDS = 5.0


class ControlOutcome(StrEnum):
    ACCEPTED_CURRENT_TURN = "accepted_current_turn"
    REJECTED = "rejected"
    UNKNOWN_DELIVERY = "unknown_delivery"


class RejectCode(StrEnum):
    TURN_ENDED = "turn_ended"
    NOT_ACCEPTED_IN_TIME = "not_accepted_in_time"
    NOT_STEERABLE = "not_steerable"
    TOO_LARGE = "too_large"
    DUPLICATE_MISMATCH = "duplicate_mismatch"
    BACKPRESSURE = "backpressure"
    SESSION_CLOSED = "session_closed"
    UNKNOWN_SESSION = "unknown_session"
    PERMISSION_DENIED = "permission_denied"


class SteerRejectedError(Exception):
    def __init__(self, code: RejectCode) -> None:
        super().__init__(code.value)
        self.code = code


@runtime_checkable
class SteerableSession(Protocol):
    """A session that can take input into its CURRENT turn at its next safe boundary.

    ``steer`` returns once the harness accepted the input, raises :class:`SteerRejectedError`
    otherwise, and never starts a turn. Every pending input resolves when the session closes.
    """

    async def steer(self, text: str, *, input_id: str, wait: float) -> None: ...


class InterruptibleSession(Protocol):
    async def interrupt(self) -> None: ...


class StreamFacts(Protocol):
    """What the driver's stream capture already knows; read, never queried from the model."""

    tool_calls: int
    context_tokens: int | None
    last_event_at: float | None

    @property
    def open_tool(self) -> tuple[str, float] | None: ...

    @property
    def result_message(self) -> object: ...


class ReceiptPayload(TypedDict):
    command_id: str
    task: int
    outcome: str
    code: str | None
    mode: str
    accepted_at: str | None


class SessionFacts(TypedDict):
    task: int
    ticket: int | None
    phase: str
    harness: str
    model: str
    state: str
    steerable: bool
    pending_inputs: int
    elapsed_seconds: float
    tool_calls: int
    open_tool: str | None
    open_tool_seconds: float | None
    last_event_age_seconds: float | None
    context_tokens: int | None


@dataclass(frozen=True, slots=True)
class LiveTask:
    pk: int
    ticket: int | None
    phase: str
    harness: str
    model: str


@dataclass(frozen=True, slots=True)
class ControlReceipt:
    command_id: str
    task: int
    outcome: ControlOutcome
    code: RejectCode | None = None
    accepted_at: str | None = None

    @classmethod
    def rejected(cls, command_id: str, task: int, code: RejectCode) -> "ControlReceipt":
        return cls(command_id, task, ControlOutcome.REJECTED, code)

    def as_payload(self) -> ReceiptPayload:
        return {
            "command_id": self.command_id,
            "task": self.task,
            "outcome": self.outcome.value,
            "code": self.code.value if self.code else None,
            "mode": "active",
            "accepted_at": self.accepted_at,
        }


def frame_operator_input(text: str, command_id: str) -> str:
    return (
        f"[TeaTree operator input id={command_id}] From the operator supervising this task, delivered mid-run.\n"
        f"{text}\n"
        "[end operator input: continue your task; your final message must still carry your result envelope]"
    )


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class LiveSessionController:
    """The one command owner of a live session: admission, idempotency and its receipts.

    Every method that touches the session runs on the session's own event loop.
    """

    def __init__(self, task: LiveTask, capture: StreamFacts) -> None:
        self.task = task
        self._capture = capture
        self._session: InterruptibleSession | None = None
        self._closed = False
        self._started = monotonic()
        self._receipts: dict[str, tuple[str, asyncio.Future[ControlReceipt]]] = {}
        self._in_flight: set[asyncio.Future[ControlReceipt]] = set()

    def bind(self, session: InterruptibleSession) -> None:
        self._session = session

    def close(self) -> None:
        self._closed = True

    async def interrupt(self) -> None:
        """Refuse new commands, then stop the running turn."""
        self.close()
        if self._session is not None:
            await self._session.interrupt()

    @property
    def steerable(self) -> bool:
        return not self._closed and isinstance(self._session, SteerableSession)

    @property
    def state(self) -> str:
        if self._closed:
            return "closed"
        if self._capture.result_message is not None:
            return "finishing"
        return "busy" if self._capture.last_event_at is not None else "starting"

    def facts(self) -> SessionFacts:
        now = monotonic()
        open_tool = self._capture.open_tool
        last_event = self._capture.last_event_at
        return {
            "task": self.task.pk,
            "ticket": self.task.ticket,
            "phase": self.task.phase,
            "harness": self.task.harness,
            "model": self.task.model,
            "state": self.state,
            "steerable": self.steerable,
            "pending_inputs": len(self._in_flight),
            "elapsed_seconds": round(now - self._started, 1),
            "tool_calls": self._capture.tool_calls,
            "open_tool": open_tool[0] if open_tool else None,
            "open_tool_seconds": round(now - open_tool[1], 1) if open_tool else None,
            "last_event_age_seconds": round(now - last_event, 1) if last_event is not None else None,
            "context_tokens": self._capture.context_tokens,
        }

    def known_receipt(self, command_id: str, text: str) -> ControlReceipt | None:
        """The settled receipt of an earlier identical command, or the refusal of a reused id."""
        entry = self._receipts.get(command_id)
        if entry is None:
            return None
        digest, settled = entry
        if digest != _digest(text):
            return ControlReceipt.rejected(command_id, self.task.pk, RejectCode.DUPLICATE_MISMATCH)
        return settled.result() if settled.done() and not settled.cancelled() else None

    async def steer(self, text: str, *, command_id: str, wait: float) -> ControlReceipt:
        entry = self._receipts.get(command_id)
        if entry is not None:
            if entry[0] != _digest(text):
                return ControlReceipt.rejected(command_id, self.task.pk, RejectCode.DUPLICATE_MISMATCH)
            return await asyncio.shield(entry[1])
        refusal = self._admission_refusal(text)
        if refusal is not None:
            return ControlReceipt.rejected(command_id, self.task.pk, refusal)
        settled: asyncio.Future[ControlReceipt] = asyncio.get_running_loop().create_future()
        self._receipts[command_id] = (_digest(text), settled)
        self._in_flight.add(settled)
        try:
            receipt = await self._deliver(text, command_id, wait)
        except asyncio.CancelledError:
            settled.cancel()
            raise
        finally:
            self._in_flight.discard(settled)
        settled.set_result(receipt)
        return receipt

    def _admission_refusal(self, text: str) -> RejectCode | None:
        if self._closed:
            return RejectCode.SESSION_CLOSED
        if not isinstance(self._session, SteerableSession):
            return RejectCode.NOT_STEERABLE
        if len(text.encode()) > MAX_INPUT_BYTES:
            return RejectCode.TOO_LARGE
        if len(self._in_flight) >= MAX_PENDING_INPUTS or len(self._receipts) >= MAX_RECEIPTS:
            return RejectCode.BACKPRESSURE
        return None

    async def _deliver(self, text: str, command_id: str, wait: float) -> ControlReceipt:
        session = self._session
        if not isinstance(session, SteerableSession):
            return ControlReceipt.rejected(command_id, self.task.pk, RejectCode.NOT_STEERABLE)
        try:
            await session.steer(frame_operator_input(text, command_id), input_id=command_id, wait=wait)
        except SteerRejectedError as exc:
            return ControlReceipt.rejected(command_id, self.task.pk, exc.code)
        accepted_at = datetime.now(UTC).isoformat(timespec="seconds")
        return ControlReceipt(command_id, self.task.pk, ControlOutcome.ACCEPTED_CURRENT_TURN, accepted_at=accepted_at)

    async def drain(self) -> None:
        """Give in-flight commands a bounded moment to settle once their session has resolved them."""
        if self._in_flight:
            await asyncio.wait(set(self._in_flight), timeout=_DRAIN_SECONDS)
