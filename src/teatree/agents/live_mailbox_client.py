"""Socket client for task and attended live mailbox identities."""

import asyncio
import json
import os
import socket
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self, cast

from teatree.agents.live_control import PeerIdentity
from teatree.agents.live_delivery import MAILBOX_AUTH_ENV, MAILBOX_SOCKET_ENV, InboxPage, MailboxIdentity, SendReceipt
from teatree.agents.live_federation import FederationUnavailableError, _require_trusted_worker, live_dir

_MAX_RESPONSE_BYTES = 2_000_000
_MAX_JOIN_RESPONSE_BYTES = 4096
_INVALID_RESPONSE = "Live mailbox returned an invalid response"
_UNAVAILABLE = "Live mailbox is unavailable; this TeaTree session may have ended"
_INVALID_JOIN = "Interactive mailbox join returned invalid data"
_NO_JOIN = "No live worker accepted the interactive mailbox join"


class MailboxError(Exception):
    """A broker refusal or transport failure."""


class NoLiveWorkerError(MailboxError):
    pass


def _joined_identity(response: bytes) -> tuple[str, str]:
    try:
        payload = json.loads(response)
    except (ValueError, UnicodeDecodeError) as exc:
        raise MailboxError(_INVALID_JOIN) from exc
    if not isinstance(payload, dict):
        raise MailboxError(_INVALID_JOIN)
    if "error" in payload:
        message = f"{payload.get('code', 'rejected')}: {payload['error']}"
        raise MailboxError(message)
    result = payload.get("result")
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("address"), str)
        or not isinstance(result.get("token"), str)
    ):
        raise MailboxError(_INVALID_JOIN)
    return result["address"], result["token"]


@dataclass(slots=True)
class InteractiveMailboxLease:
    socket_path: Path
    address: str
    token: str = field(repr=False)
    _socket: socket.socket = field(repr=False)

    @classmethod
    def connect(cls, directory: Path | None = None) -> Self:
        root = directory or live_dir()
        for path in sorted(root.glob("w-*.sock")):
            connected = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                connected.settimeout(5)
                connected.connect(str(path))
                _require_trusted_worker(path, connected)
                connected.sendall(b'{"method":"peer.join"}\n')
                response = b""
                while not response.endswith(b"\n") and len(response) < _MAX_JOIN_RESPONSE_BYTES:
                    chunk = connected.recv(_MAX_JOIN_RESPONSE_BYTES - len(response))
                    if not chunk:
                        break
                    response += chunk
            except (OSError, FederationUnavailableError):
                connected.close()
                continue
            except Exception:
                connected.close()
                raise
            try:
                address, token = _joined_identity(response)
                connected.settimeout(None)
            except Exception:
                connected.close()
                raise
            return cls(path, address, token, connected)
        raise NoLiveWorkerError(_NO_JOIN)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self._socket.close()

    def mcp_env(self) -> dict[str, str]:
        return MailboxIdentity(self.socket_path, self.address, self.token).mcp_env()


class MailboxClient:
    def __init__(self, socket_path: Path, token: str) -> None:
        self.socket_path = socket_path
        self.token = token

    def __repr__(self) -> str:
        return f"MailboxClient(socket_path={self.socket_path!r})"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "MailboxClient | None":
        source = os.environ if env is None else env
        socket_path = source.get(MAILBOX_SOCKET_ENV)
        token = source.get(MAILBOX_AUTH_ENV)
        if not socket_path or not token:
            return None
        return cls(Path(socket_path), token)

    async def _request(self, method: str, **arguments: object) -> object:
        try:
            reader, writer = await asyncio.open_unix_connection(str(self.socket_path), limit=_MAX_RESPONSE_BYTES)
            try:
                request = {"token": self.token, "method": method, **arguments}
                writer.write(json.dumps(request).encode() + b"\n")
                await writer.drain()
                try:
                    raw = await reader.readline()
                except ValueError as exc:
                    raise MailboxError(_INVALID_RESPONSE) from exc
                if not raw or len(raw) > _MAX_RESPONSE_BYTES:
                    raise MailboxError(_INVALID_RESPONSE)
                try:
                    response = json.loads(raw)
                except (ValueError, RecursionError) as exc:
                    raise MailboxError(_INVALID_RESPONSE) from exc
                if not isinstance(response, dict):
                    raise MailboxError(_INVALID_RESPONSE)
                if "error" in response:
                    msg = str(response["error"])
                    raise MailboxError(msg)
                if "result" not in response:
                    raise MailboxError(_INVALID_RESPONSE)
                return response["result"]
            finally:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()
        except (OSError, ValueError) as exc:
            raise MailboxError(_UNAVAILABLE) from exc

    async def self_identity(self) -> PeerIdentity:
        """Return this session's TeaTree-assigned address and harness; an attended session has no ticket."""
        return cast("PeerIdentity", await self._request("self"))

    async def peers(self) -> list[PeerIdentity]:
        """List live TeaTree tasks on this host with ticket, phase, harness and receive status."""
        return cast("list[PeerIdentity]", await self._request("peers"))

    async def send(self, to: str, text: str, key: str, *, timeout_seconds: float = 20) -> SendReceipt:
        """Send one idempotent message to an addressed live peer and wait for session acceptance.

        The receipt's outcome is accepted_current_turn (pushed into the peer's running turn), accepted_new_turn
        (an idle Claude session started a new turn), accepted_inbox_read (the peer read its inbox), or rejected
        with a code: not_accepted_in_time (still queued in the peer's inbox), not_idle, stale_command,
        session_closed or dropped_unread. Acceptance is not obedience. Retry with the same key to keep waiting;
        a new key sends a second copy. A push-only peer has no inbox copy; its receipt always says
        queued_in_inbox false. A pydantic_ai peer can use native inbox tools without an MCP session.
        An idle Claude peer can be woken once during its bounded live window. An uncertain push is unknown_delivery.
        """
        return cast(
            "SendReceipt", await self._request("send", to=to, text=text, key=key, timeout_seconds=timeout_seconds)
        )

    async def _read_and_ack(self, method: str, **arguments: object) -> InboxPage:
        page = cast("InboxPage", await self._request(method, **arguments))
        if page["messages"]:
            await self._request("ack", ids=[row["id"] for row in page["messages"]])
        return page

    async def inbox(self, *, after_id: int = 0, limit: int = 50) -> InboxPage:
        """Read addressed messages in order; this acknowledges them to their senders; keep next_cursor."""
        return await self._read_and_ack("inbox", after_id=after_id, limit=limit)

    async def wait(self, *, after_id: int = 0, timeout_seconds: float = 20) -> InboxPage:
        """Wait at most 20 seconds for mail; this acknowledges what it returns; a parked task is never woken."""
        return await self._read_and_ack("wait", after_id=after_id, timeout_seconds=timeout_seconds)
