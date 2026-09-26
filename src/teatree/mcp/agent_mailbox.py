"""MCP tools for TeaTree-owned live sessions; the broker owns all identities."""

import asyncio
import json
import os
from contextlib import suppress
from pathlib import Path
from typing import cast

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from teatree.agents.live_mailbox import InboxPage, SendReceipt

_READ = ToolAnnotations(read_only_hint=True)
_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False)
_MAX_RESPONSE_BYTES = 2_000_000  # 100 valid 16-KiB messages plus envelope metadata

TOOL_SEAMS = {"agent_mailbox_send": "LiveMailboxBroker._send — authenticated, room-scoped, live-only delivery"}

INSTRUCTIONS = (
    "- agent_mailbox_self(): your TeaTree-assigned address and harness.\n"
    "- agent_mailbox_peers(): other currently live TeaTree tasks on this ticket.\n"
    "- agent_mailbox_send(to, text, key): send a targeted message; reuse key for a safe retry.\n"
    "- agent_mailbox_inbox(after_id, limit): read your messages in order and advance the cursor.\n"
    "- agent_mailbox_wait(after_id, timeout_seconds): wait at most 20 seconds; idle sessions are not woken."
)


class MailboxClient:
    def __init__(self, socket_path: Path, token: str) -> None:
        self.socket_path = socket_path
        self.token = token

    @classmethod
    def from_env(cls) -> "MailboxClient | None":
        socket_path = os.environ.get("T3_AGENT_MAILBOX_SOCKET")
        token = os.environ.get("T3_AGENT_MAILBOX_TOKEN")
        if not socket_path or not token:
            return None
        return cls(Path(socket_path), token)

    async def _request(self, method: str, **arguments: object) -> object:
        try:
            reader, writer = await asyncio.open_unix_connection(str(self.socket_path), limit=_MAX_RESPONSE_BYTES)
        except (OSError, ConnectionError) as exc:
            msg = "Live mailbox is unavailable; this TeaTree session may have ended"
            raise ToolError(msg) from exc
        try:
            request = {"token": self.token, "method": method, **arguments}
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            raw = await reader.readline()
            if not raw or len(raw) > _MAX_RESPONSE_BYTES:
                msg = "Live mailbox returned an invalid response"
                raise ToolError(msg)
            response = json.loads(raw)
            if not isinstance(response, dict):
                msg = "Live mailbox returned an invalid response"
                raise ToolError(msg)
            if "error" in response:
                msg = str(response["error"])
                raise ToolError(msg)
            if "result" not in response:
                msg = "Live mailbox returned an invalid response"
                raise ToolError(msg)
            return response["result"]
        finally:
            writer.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await writer.wait_closed()

    async def self_identity(self) -> dict[str, str]:
        """Return the address TeaTree assigned to this running task."""
        return cast("dict[str, str]", await self._request("self"))

    async def peers(self) -> list[dict[str, str]]:
        """List currently live TeaTree tasks on this ticket."""
        return cast("list[dict[str, str]]", await self._request("peers"))

    async def send(self, to: str, text: str, key: str) -> SendReceipt:
        """Send one idempotent message to a live peer on this ticket."""
        return cast("SendReceipt", await self._request("send", to=to, text=text, key=key))

    async def inbox(self, *, after_id: int = 0, limit: int = 50) -> InboxPage:
        """Read messages addressed to this task and retain next_cursor."""
        return cast("InboxPage", await self._request("inbox", after_id=after_id, limit=limit))

    async def wait(self, *, after_id: int = 0, timeout_seconds: float = 20) -> InboxPage:
        """Wait briefly for mail without waking an idle or parked task."""
        result = await self._request("wait", after_id=after_id, timeout_seconds=timeout_seconds)
        return cast("InboxPage", result)


def register(server: MCPServer) -> bool:
    client = MailboxClient.from_env()
    if client is None:
        return False
    server.add_tool(client.self_identity, name="agent_mailbox_self", annotations=_READ)
    server.add_tool(client.peers, name="agent_mailbox_peers", annotations=_READ)
    server.add_tool(client.send, name="agent_mailbox_send", annotations=_WRITE)
    server.add_tool(client.inbox, name="agent_mailbox_inbox", annotations=_READ)
    server.add_tool(client.wait, name="agent_mailbox_wait", annotations=_READ)
    return True
