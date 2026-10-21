"""Socket client for a task-owned live mailbox identity."""

import asyncio
import json
import os
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import cast

from teatree.agents.live_control import PeerIdentity
from teatree.agents.live_delivery import InboxPage, SendReceipt

_MAX_RESPONSE_BYTES = 2_000_000


class MailboxError(Exception):
    """A broker refusal or transport failure."""


class MailboxClient:
    def __init__(self, socket_path: Path, token: str) -> None:
        self.socket_path = socket_path
        self.token = token

    def __repr__(self) -> str:
        return f"MailboxClient(socket_path={self.socket_path!r})"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "MailboxClient | None":
        source = os.environ if env is None else env
        socket_path = source.get("T3_AGENT_MAILBOX_SOCKET")
        token = source.get("T3_AGENT_MAILBOX_TOKEN")
        if not socket_path or not token:
            return None
        return cls(Path(socket_path), token)

    async def _request(self, method: str, **arguments: object) -> object:
        try:
            reader, writer = await asyncio.open_unix_connection(str(self.socket_path), limit=_MAX_RESPONSE_BYTES)
        except (OSError, ConnectionError) as exc:
            msg = "Live mailbox is unavailable; this TeaTree session may have ended"
            raise MailboxError(msg) from exc
        try:
            request = {"token": self.token, "method": method, **arguments}
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            raw = await reader.readline()
            if not raw or len(raw) > _MAX_RESPONSE_BYTES:
                msg = "Live mailbox returned an invalid response"
                raise MailboxError(msg)
            response = json.loads(raw)
            if not isinstance(response, dict):
                msg = "Live mailbox returned an invalid response"
                raise MailboxError(msg)
            if "error" in response:
                msg = str(response["error"])
                raise MailboxError(msg)
            if "result" not in response:
                msg = "Live mailbox returned an invalid response"
                raise MailboxError(msg)
            return response["result"]
        finally:
            writer.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await writer.wait_closed()

    async def self_identity(self) -> PeerIdentity:
        """Return the address TeaTree assigned to this running task."""
        return cast("PeerIdentity", await self._request("self"))

    async def peers(self) -> list[PeerIdentity]:
        """List live TeaTree tasks on this host."""
        return cast("list[PeerIdentity]", await self._request("peers"))

    async def send(self, to: str, text: str, key: str, *, timeout_seconds: float = 20) -> SendReceipt:
        """Send one idempotent message to an addressed live peer and wait for session acceptance.

        The receipt's outcome is accepted_current_turn (pushed into the peer's running turn), accepted_inbox_read
        (the peer read its inbox), or rejected with a code: not_accepted_in_time (still queued in the peer's inbox),
        session_closed or dropped_unread. Acceptance is not obedience. Retry with the same key to keep waiting;
        a new key sends a second copy.
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
        """Read messages addressed to this task, which acknowledges them to their senders; keep next_cursor."""
        return await self._read_and_ack("inbox", after_id=after_id, limit=limit)

    async def wait(self, *, after_id: int = 0, timeout_seconds: float = 20) -> InboxPage:
        """Wait briefly for mail, which acknowledges what it returns; an idle or parked task is not woken."""
        return await self._read_and_ack("wait", after_id=after_id, timeout_seconds=timeout_seconds)
