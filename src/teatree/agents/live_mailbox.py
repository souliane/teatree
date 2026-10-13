"""Process-local, task-owned live mailbox and operator ingress, served over the worker's published Unix socket."""

import asyncio
import atexit
import json
import logging
import os
import secrets
import threading
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, NoReturn, Self, TypedDict, cast

from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents.live_control import (
    MAX_INPUT_BYTES,
    ControlOutcome,
    LiveTask,
    PeerIdentity,
    RejectCode,
    frame_peer_message,
)
from teatree.agents.live_delivery import PeerDelivery, SendReceipt
from teatree.agents.live_ingress import IngressRefusedError, OperatorIngress
from teatree.agents.live_registry import LiveSessionRegistry, shared_registry
from teatree.paths import control_db_dir

if TYPE_CHECKING:
    from claude_agent_sdk.types import McpServerConfig, McpStdioServerConfig

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = MAX_INPUT_BYTES
_MAX_WAIT_SECONDS = 20
_MAX_INBOX_MESSAGES = 1_000
_MAX_REQUEST_BYTES = 128_000  # JSON escaping can expand a valid 16-KiB body sixfold
_MAX_SOCKET_PATH_BYTES = 100
_MAX_KEY_BYTES = 128
_MAX_SENT_KEYS = 1_000
_MAX_PAGE_SIZE = 100
_MAX_PAGE_BYTES = 1_800_000
MAX_WAITING_SENDS = 4
type MailboxRequest = Mapping[str, object]


InboxMessage = TypedDict(
    "InboxMessage",
    {"id": int, "text": str, "from": str, "from_harness": str, "from_label": str},
)


class InboxPage(TypedDict):
    messages: list[InboxMessage]
    next_cursor: int
    truncated: bool


def _reject(message: str) -> NoReturn:
    raise ValueError(message)


def _send_fields(request: MailboxRequest) -> tuple[str, str, str, float]:
    to, body, key = request.get("to"), request.get("text"), request.get("key")
    timeout = request.get("timeout_seconds", _MAX_WAIT_SECONDS)
    if not isinstance(to, str) or not isinstance(body, str) or not isinstance(key, str):
        _reject("Mailbox send requires to, text, and key strings")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= _MAX_WAIT_SECONDS:
        _reject("Mailbox acceptance timeout must be between 0 and 20 seconds")
    if not key or len(key.encode()) > _MAX_KEY_BYTES:
        _reject("Mailbox key must be 1-128 bytes")
    if not body or len(body.encode()) > _MAX_BODY_BYTES:
        _reject("Mailbox text must be 1-16384 bytes")
    return to, body, key, float(timeout)


@dataclass(frozen=True, slots=True)
class MailboxIdentity:
    socket_path: Path
    address: str
    token: str

    def mcp_env(self) -> dict[str, str]:
        return {"T3_AGENT_MAILBOX_SOCKET": str(self.socket_path), "T3_AGENT_MAILBOX_TOKEN": self.token}


@dataclass(slots=True)
class _Participant:
    address: str
    token: str
    room: str
    harness: str
    label: str
    task: int | None = None
    inbox: deque[InboxMessage] = field(default_factory=lambda: deque(maxlen=_MAX_INBOX_MESSAGES))
    sent: dict[str, PeerDelivery] = field(default_factory=dict)
    dropped_before: int = 0
    waiting: int = 0


def live_dir() -> Path:
    """Where every worker on this host publishes its socket: the control-DB volume all services mount."""
    return control_db_dir(os.environ) / "live"


def _publish_path(directory: Path) -> Path:
    path = directory / f"w-{secrets.token_hex(4)}.sock"
    if len(os.fsencode(str(path))) > _MAX_SOCKET_PATH_BYTES:
        msg = f"Live socket path {path} exceeds {_MAX_SOCKET_PATH_BYTES} bytes"
        raise RuntimeError(msg)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    return _publish_path(directory) if path.exists() else path


class LiveMailboxBroker:
    """Own only sessions currently running in this worker process."""

    def __init__(
        self,
        *,
        runtime_dir: Path | None = None,
        ingress: OperatorIngress | None = None,
        registry: Callable[[], LiveSessionRegistry] = shared_registry,
    ) -> None:
        self._runtime_dir = runtime_dir
        self._registry = registry
        self._ingress = ingress or OperatorIngress(registry)
        self._socket_path: Path | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._lock = threading.RLock()
        self._by_token: dict[str, _Participant] = {}
        self._by_address: dict[str, _Participant] = {}
        self._next_message_id = 0
        self._delivery: dict[int, PeerDelivery] = {}

    @property
    def socket_path(self) -> Path:
        if self._socket_path is None:
            msg = "Live mailbox broker has not started"
            raise RuntimeError(msg)
        return self._socket_path

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._ready.clear()
        self._startup_error = None
        self._socket_path = _publish_path(self._runtime_dir or live_dir())
        self._thread = threading.Thread(target=self._run, name="t3-live-mailbox", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            msg = "Live mailbox broker did not start"
            raise RuntimeError(msg)
        if self._startup_error is not None:
            self._thread.join(timeout=5)
            self._thread = None
            self._socket_path = None
            msg = "Live mailbox broker failed to start"
            raise RuntimeError(msg) from self._startup_error

    def close(self) -> None:
        loop, stop, thread = self._loop, self._stop, self._thread
        if loop is not None and not loop.is_closed() and stop is not None and thread is not None and thread.is_alive():
            loop.call_soon_threadsafe(stop.set)
            thread.join(timeout=5)
        with self._lock:
            self._by_token.clear()
            self._by_address.clear()
            self._delivery.clear()
        self._thread = None
        self._loop = None
        self._stop = None

    def register(self, *, room: str, harness: str, label: str, task: int | None = None) -> MailboxIdentity:
        if not room or not harness or not label:
            msg = "Mailbox room, harness, and label are required"
            raise ValueError(msg)
        self.start()
        participant = _Participant(
            address=str(uuid.uuid4()),
            token=secrets.token_urlsafe(32),
            room=room,
            harness=harness,
            label=label,
            task=task,
        )
        with self._lock:
            self._by_token[participant.token] = participant
            self._by_address[participant.address] = participant
        return MailboxIdentity(self.socket_path, participant.address, participant.token)

    def unregister(self, address: str) -> None:
        with self._lock:
            participant = self._by_address.pop(address, None)
            if participant is None:
                return
            self._by_token.pop(participant.token, None)
            received = [row["id"] for row in participant.inbox]
            sent = [delivery.message_id for delivery in participant.sent.values()]
        loop = self._loop
        if loop is not None and not loop.is_closed():
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(self._recipient_left, received)
                loop.call_soon_threadsafe(self._sender_left, sent)

    def _recipient_left(self, message_ids: list[int]) -> None:
        for message_id in message_ids:
            if (delivery := self._delivery.get(message_id)) is not None:
                delivery.settle(ControlOutcome.REJECTED, RejectCode.SESSION_CLOSED)

    def _sender_left(self, message_ids: list[int]) -> None:
        for message_id in message_ids:
            self._delivery.pop(message_id, None)

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as exc:  # noqa: BLE001 - propagate startup failure to waiting callers
            self._startup_error = exc
            self._ready.set()
        finally:
            if self._socket_path is not None:
                self._socket_path.unlink(missing_ok=True)

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path), limit=_MAX_REQUEST_BYTES)
        self.socket_path.chmod(0o600)
        self._ready.set()
        async with server:
            await self._stop.wait()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            if len(raw) > _MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                _reject("Mailbox request is too large")
            request = json.loads(raw)
            if not isinstance(request, dict):
                _reject("Invalid mailbox request")
            method = request.get("method")
            if isinstance(method, str) and method.startswith("live."):
                result = await self._ingress.serve(method, request, writer.get_extra_info("socket"))
            else:
                result = await self._dispatch(request)
            response = {"result": result}
        except IngressRefusedError as exc:
            response = {"error": str(exc), "code": exc.code.value}
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            response = {"error": str(exc)}
        except Exception:  # noqa: BLE001 - never expose broker internals to an MCP child
            response = {"error": "Mailbox request failed"}
        try:
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def _dispatch(self, request: MailboxRequest) -> object:
        token = request.get("token")
        if not isinstance(token, str):
            _reject("Invalid mailbox identity")
        with self._lock:
            participant = self._by_token.get(token)
        if participant is None:
            _reject("Invalid mailbox identity")
        method = request.get("method")
        if method == "self":
            return self._identity(participant)
        if method == "peers":
            return self._peers(participant)
        if method == "send":
            return await self._send(participant, request)
        if method == "ack":
            return self._ack(participant, request)
        if method == "inbox":
            return self._inbox(participant, request)
        if method == "wait":
            return await self._wait(participant, request)
        _reject("Unknown mailbox method")

    @staticmethod
    def _identity(participant: _Participant) -> PeerIdentity:
        return {
            "address": participant.address,
            "harness": participant.harness,
            "label": participant.label,
        }

    def _require_active(self, participant: _Participant) -> None:
        if self._by_address.get(participant.address) is not participant:
            _reject("Invalid mailbox identity")

    def _peers(self, participant: _Participant) -> list[PeerIdentity]:
        with self._lock:
            self._require_active(participant)
            return [
                self._identity(peer)
                for peer in self._by_address.values()
                if peer.room == participant.room and peer.address != participant.address
            ]

    async def _send(self, participant: _Participant, request: MailboxRequest) -> SendReceipt:
        to, body, key, timeout = _send_fields(request)
        with self._lock:
            self._require_active(participant)
            if participant.waiting >= MAX_WAITING_SENDS:
                _reject(f"Mailbox send refused: {MAX_WAITING_SENDS} sends from this task are waiting for acceptance")
            delivery = participant.sent.get(key)
            if delivery is None:
                delivery = self._enqueue(participant, to, body)
                participant.sent[key] = delivery
            elif (delivery.to, delivery.text) != (to, body):
                _reject("Mailbox key already used for another message")
            recipient = self._by_address.get(to)
            participant.waiting += 1
        try:
            return await self._accepted(delivery, recipient.task if recipient else None, timeout)
        finally:
            participant.waiting -= 1

    def _enqueue(self, sender: _Participant, to: str, body: str) -> PeerDelivery:
        if len(sender.sent) >= _MAX_SENT_KEYS:
            _reject("Mailbox send limit reached for this task")
        recipient = self._by_address.get(to)
        if recipient is None:
            _reject("Mailbox recipient is not active")
        if recipient.room != sender.room:
            _reject("Mailbox recipient is outside this room")
        self._next_message_id += 1
        message_id = self._next_message_id
        if len(recipient.inbox) == recipient.inbox.maxlen:
            evicted = recipient.inbox[0]["id"]
            if (unread := self._delivery.get(evicted)) is not None:
                unread.settle(ControlOutcome.REJECTED, RejectCode.DROPPED_UNREAD)
            recipient.dropped_before = evicted
        recipient.inbox.append(
            {
                "id": message_id,
                "from": sender.address,
                "from_harness": sender.harness,
                "from_label": sender.label,
                "text": body,
            }
        )
        framed = frame_peer_message(body, message_id=message_id, sender=self._identity(sender))
        delivery = PeerDelivery(message_id, to, body, framed, asyncio.get_running_loop().create_future())
        self._delivery[message_id] = delivery
        return delivery

    async def _accepted(self, delivery: PeerDelivery, task: int | None, wait: float) -> SendReceipt:
        """Push into the recipient's running turn while its inbox read can still accept first."""
        if not delivery.settled.done():
            if task is not None and delivery.push is None:
                delivery.push = asyncio.create_task(self._push_into_turn(delivery, task, wait))
            try:
                await asyncio.wait_for(asyncio.shield(delivery.settled), wait)
            except TimeoutError:
                if delivery.push is not None:
                    await asyncio.wait({delivery.push})
        return delivery.receipt()

    async def _push_into_turn(self, delivery: PeerDelivery, task: int, wait: float) -> None:
        command_id = f"peer-{delivery.message_id}"
        try:
            receipt = await self._registry().push(task, delivery.framed, command_id=command_id, wait=wait)
        finally:
            delivery.push = None
        if receipt.outcome is ControlOutcome.ACCEPTED_CURRENT_TURN:
            delivery.settle(ControlOutcome.ACCEPTED_CURRENT_TURN)

    def _ack(self, participant: _Participant, request: MailboxRequest) -> bool:
        ids = request.get("ids")
        if (
            not isinstance(ids, list)
            or len(ids) > _MAX_PAGE_SIZE
            or any(
                not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0 for message_id in ids
            )
        ):
            _reject("Mailbox acknowledgement needs up to 100 positive message IDs")
        with self._lock:
            self._require_active(participant)
            for row in participant.inbox:
                if row["id"] in ids and (delivery := self._delivery.get(row["id"])) is not None:
                    delivery.settle(ControlOutcome.ACCEPTED_INBOX_READ)
        return True

    def _inbox(self, participant: _Participant, request: MailboxRequest) -> InboxPage:
        after_id, limit = request.get("after_id", 0), request.get("limit", 50)
        if not isinstance(after_id, int) or isinstance(after_id, bool) or after_id < 0:
            _reject("Mailbox cursor must be nonnegative")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_PAGE_SIZE:
            _reject("Mailbox page size must be 1-100")
        with self._lock:
            self._require_active(participant)
            messages: list[InboxMessage] = []
            encoded_bytes = 0
            for row in participant.inbox:
                if row["id"] <= after_id:
                    continue
                row_bytes = len(json.dumps(row).encode())
                if messages and encoded_bytes + row_bytes > _MAX_PAGE_BYTES:
                    break
                messages.append(row)
                encoded_bytes += row_bytes
                if len(messages) >= limit:
                    break
            truncated = after_id < participant.dropped_before
        return {
            "messages": messages,
            "next_cursor": messages[-1]["id"] if messages else after_id,
            "truncated": truncated,
        }

    async def _wait(self, participant: _Participant, request: MailboxRequest) -> InboxPage:
        timeout = request.get("timeout_seconds", _MAX_WAIT_SECONDS)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 <= timeout <= _MAX_WAIT_SECONDS:
            _reject("Mailbox wait timeout must be between 0 and 20 seconds")
        deadline = monotonic() + timeout
        while True:
            result = self._inbox(participant, request)
            if result["messages"] or monotonic() >= deadline:
                return result
            await asyncio.sleep(min(0.05, deadline - monotonic()))


_shared_brokers: dict[int, LiveMailboxBroker] = {}
_shared_lock = threading.Lock()


def shared_broker() -> LiveMailboxBroker:
    pid = os.getpid()
    with _shared_lock:
        if pid not in _shared_brokers:
            broker = LiveMailboxBroker()
            broker.start()
            _shared_brokers[pid] = broker
        return _shared_brokers[pid]


@contextmanager
def bound_task_mailbox(
    options: ClaudeAgentOptions,
    task: LiveTask,
    *,
    broker: LiveMailboxBroker | None = None,
) -> Iterator[MailboxIdentity | None]:
    """Bind a running TeaTree task to its own stdio MCP child; revoke it on exit."""
    original = options.mcp_servers
    servers = original if isinstance(original, dict) else {}
    teatree = servers.get("teatree")
    if not isinstance(teatree, dict) or not isinstance(teatree.get("command"), str):
        yield None
        return
    teatree_stdio = cast("McpStdioServerConfig", teatree)
    label = f"{task.phase} task {task.pk}"
    try:
        active = broker or shared_broker()
        identity = active.register(room=str(task.ticket), harness=task.harness, label=label, task=task.pk)
    except Exception:
        logger.warning("live mailbox unavailable for %s; dispatching without one", label, exc_info=True)
        yield None
        return
    env = teatree_stdio.get("env")
    options.mcp_servers = cast(
        "dict[str, McpServerConfig]",
        {
            **servers,
            "teatree": {**teatree_stdio, "env": {**(env if isinstance(env, dict) else {}), **identity.mcp_env()}},
        },
    )
    try:
        yield identity
    finally:
        options.mcp_servers = original
        active.unregister(identity.address)


def reset_shared_brokers() -> None:
    """Close and forget worker-local brokers between tests or at process exit."""
    with _shared_lock:
        for broker in _shared_brokers.values():
            broker.close()
        _shared_brokers.clear()


atexit.register(reset_shared_brokers)
