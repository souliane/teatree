"""Process-local, task-owned live mailbox served over a private Unix socket."""

import asyncio
import atexit
import json
import logging
import os
import secrets
import tempfile
import threading
import uuid
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, NoReturn, Self, TypedDict, cast

from claude_agent_sdk import ClaudeAgentOptions

if TYPE_CHECKING:
    from claude_agent_sdk.types import McpServerConfig, McpStdioServerConfig

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 16_384
_MAX_WAIT_SECONDS = 20
_MAX_INBOX_MESSAGES = 1_000
_MAX_REQUEST_BYTES = 128_000  # JSON escaping can expand a valid 16-KiB body sixfold
_MAX_SOCKET_PATH_BYTES = 100
_MAX_KEY_BYTES = 128
_MAX_SENT_KEYS = 1_000
_MAX_PAGE_SIZE = 100
_MAX_PAGE_BYTES = 1_800_000
type MailboxRequest = Mapping[str, object]


InboxMessage = TypedDict(
    "InboxMessage",
    {"id": int, "text": str, "from": str, "from_harness": str, "from_label": str},
)


class InboxPage(TypedDict):
    messages: list[InboxMessage]
    next_cursor: int
    truncated: bool


class SendReceipt(TypedDict):
    id: int
    to: str


def _reject(message: str) -> NoReturn:
    raise ValueError(message)


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
    inbox: deque[InboxMessage] = field(default_factory=lambda: deque(maxlen=_MAX_INBOX_MESSAGES))
    sent: dict[str, tuple[str, str, int]] = field(default_factory=dict)
    dropped_before: int = 0


class LiveMailboxBroker:
    """Own only sessions currently running in this worker process."""

    def __init__(self, *, runtime_dir: Path | None = None) -> None:
        self._runtime_dir = runtime_dir
        self._directory: Path | None = None
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
        directory = Path(tempfile.mkdtemp(prefix="t3-mailbox-", dir=self._runtime_dir))
        if len(os.fsencode(str(directory / "live.sock"))) > _MAX_SOCKET_PATH_BYTES:
            directory.rmdir()
            directory = Path(tempfile.mkdtemp(prefix="t3-mailbox-", dir="/tmp"))
        directory.chmod(0o700)
        self._directory = directory
        self._socket_path = directory / "live.sock"
        self._thread = threading.Thread(target=self._run, name="t3-live-mailbox", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            msg = "Live mailbox broker did not start"
            raise RuntimeError(msg)
        if self._startup_error is not None:
            self._thread.join(timeout=5)
            self._thread = None
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
        self._thread = None
        self._loop = None
        self._stop = None

    def register(self, *, room: str, harness: str, label: str) -> MailboxIdentity:
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
        )
        with self._lock:
            self._by_token[participant.token] = participant
            self._by_address[participant.address] = participant
        return MailboxIdentity(self.socket_path, participant.address, participant.token)

    def unregister(self, address: str) -> None:
        with self._lock:
            participant = self._by_address.pop(address, None)
            if participant is not None:
                self._by_token.pop(participant.token, None)

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as exc:  # noqa: BLE001 - propagate startup failure to waiting callers
            self._startup_error = exc
            self._ready.set()
        finally:
            if self._socket_path is not None:
                self._socket_path.unlink(missing_ok=True)
            if self._directory is not None:
                self._directory.rmdir()

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
            result = await self._dispatch(request)
            response = {"result": result}
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
            return self._send(participant, request)
        if method == "inbox":
            return self._inbox(participant, request)
        if method == "wait":
            return await self._wait(participant, request)
        _reject("Unknown mailbox method")

    @staticmethod
    def _identity(participant: _Participant) -> dict[str, str]:
        return {
            "address": participant.address,
            "harness": participant.harness,
            "label": participant.label,
        }

    def _require_active(self, participant: _Participant) -> None:
        if self._by_address.get(participant.address) is not participant:
            _reject("Invalid mailbox identity")

    def _peers(self, participant: _Participant) -> list[dict[str, str]]:
        with self._lock:
            self._require_active(participant)
            return [
                self._identity(peer)
                for peer in self._by_address.values()
                if peer.room == participant.room and peer.address != participant.address
            ]

    def _send(self, participant: _Participant, request: MailboxRequest) -> SendReceipt:
        to, body, key = request.get("to"), request.get("text"), request.get("key")
        if not isinstance(to, str) or not isinstance(body, str) or not isinstance(key, str):
            _reject("Mailbox send requires to, text, and key strings")
        if not key or len(key.encode()) > _MAX_KEY_BYTES:
            _reject("Mailbox key must be 1-128 bytes")
        if not body or len(body.encode()) > _MAX_BODY_BYTES:
            _reject("Mailbox text must be 1-16384 bytes")
        with self._lock:
            self._require_active(participant)
            prior = participant.sent.get(key)
            if prior is not None:
                if prior[:2] != (to, body):
                    _reject("Mailbox key already used for another message")
                return {"id": prior[2], "to": to}
            if len(participant.sent) >= _MAX_SENT_KEYS:
                _reject("Mailbox send limit reached for this task")
            recipient = self._by_address.get(to)
            if recipient is None:
                _reject("Mailbox recipient is not active")
            if recipient.room != participant.room:
                _reject("Mailbox recipient is outside this room")
            self._next_message_id += 1
            message_id = self._next_message_id
            if len(recipient.inbox) == _MAX_INBOX_MESSAGES:
                recipient.dropped_before = recipient.inbox[0]["id"]
            recipient.inbox.append(
                {
                    "id": message_id,
                    "from": participant.address,
                    "from_harness": participant.harness,
                    "from_label": participant.label,
                    "text": body,
                }
            )
            participant.sent[key] = (to, body, message_id)
            return {"id": message_id, "to": to}

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
    *,
    room: str,
    harness: str,
    label: str,
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
    try:
        active = broker or shared_broker()
        identity = active.register(room=room, harness=harness, label=label)
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
