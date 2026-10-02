"""Both Slack read refusals reach the CALLER with their reason, not just the server.

``slack_mentions`` and ``slack_channel_history`` exist so an agent never reads ``[]``
as "you were not mentioned" / "the channel is quiet". Under mcp 2.1+ that only works
when the refusal is a ``ToolError``: any other exception is re-raised as
``UnexpectedToolError("Error executing tool <name>")`` with the reason left on the
server, which turns a precise refusal back into the silent-empty it replaced.

So the assertion is the round trip, not the raise: the text has to survive
``MCPServer.call_tool``. The control is the sibling assertion that a plain
``RuntimeError`` loses it — same harness, one exception class apart.
"""

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from teatree.mcp import services_slack
from teatree.types import ChannelReadRefusedError, RawAPIDict


class _RefusingBackend:
    """A backend whose two channel-scoped reads refuse exactly as the real one does."""

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_channel_history_or_refuse(self, *, channel: str, limit: int = 50) -> list[RawAPIDict]:
        _ = limit
        raise ChannelReadRefusedError(channel, "not_in_channel")


def _server() -> MCPServer:
    server = MCPServer(name="slack-refusal-probe")
    services_slack.register(server)
    return server


def _call(tool: str, args: dict[str, Any]) -> Any:
    with patch.object(services_slack, "_client", return_value=_RefusingBackend()):
        return asyncio.run(_server().call_tool(tool, args))


def test_the_unreadable_mention_queue_refusal_reaches_the_caller() -> None:
    with pytest.raises(ToolError) as refused:
        _call("slack_mentions", {})

    assert "this process holds no Socket-Mode mention queue" in str(refused.value)
    assert "slack-events.jsonl" in str(refused.value)


def test_the_channel_read_refusal_reaches_the_caller() -> None:
    with pytest.raises(ToolError) as refused:
        _call("slack_channel_history", {"channel": "#review-broadcasts"})

    assert "not_in_channel" in str(refused.value)
    assert "This is NOT an empty channel" in str(refused.value)


def test_a_plain_exception_would_have_lost_its_reason() -> None:
    # The control: the assertions above are about the exception CLASS, and this is
    # what the pre-fix shape did on the same harness — the reason never left the server.
    reason = "REASON-THAT-MUST-REACH-THE-CALLER"

    async def plain_runtime() -> list[str]:
        await asyncio.sleep(0)
        raise RuntimeError(reason)

    server = MCPServer(name="slack-refusal-control")
    server.add_tool(plain_runtime, name="plain_runtime")

    with pytest.raises(UnexpectedToolError) as unexpected:
        asyncio.run(server.call_tool("plain_runtime", {}))

    assert reason not in str(unexpected.value)


def test_a_readable_channel_still_answers() -> None:
    # Anti-vacuity: only the refusal is converted, so a channel the bot can read works.
    messages: list[RawAPIDict] = [{"text": "shipping", "ts": "1.0"}]

    class _ReadableBackend(_RefusingBackend):
        def fetch_channel_history_or_refuse(self, *, channel: str, limit: int = 50) -> list[RawAPIDict]:
            _ = channel, limit
            return list(messages)

    with patch.object(services_slack, "_client", return_value=_ReadableBackend()):
        result = asyncio.run(_server().call_tool("slack_channel_history", {"channel": "#dev"}))

    assert result.structured_content == {"result": messages}
