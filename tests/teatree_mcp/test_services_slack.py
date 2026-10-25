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

from teatree.backends.slack.bot import SlackBotBackend
from teatree.core.send_proxy import SendBlockedError, SendVerdict
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

    def list_conversations(self, *, cursor: str = "", limit: int = 100) -> RawAPIDict:
        return {"channels": [{"id": "C123"}], "next_cursor": "next", "limit": limit, "cursor": cursor}


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
        _call("slack_channel_history", {"channel": "#sample-channel"})

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


def _post(response: RawAPIDict | Exception) -> Any:
    with (
        patch.object(services_slack, "_client", return_value=_RefusingBackend()),
        patch.object(services_slack, "OnBehalfSlackEgress") as egress,
    ):
        egress.return_value.post_once.side_effect = [response]
        result = asyncio.run(
            _server().call_tool("slack_post", {"channel": "C123", "text": "hello", "idempotency_key": "k"})
        )
    assert egress.return_value.post_once.call_args.kwargs == {
        "idempotency_key": "k",
        "channel": "C123",
        "text": "hello",
        "thread_ts": "",
    }
    return result.structured_content


def test_slack_post_delegates_its_idempotency_key_to_on_behalf_egress() -> None:
    assert _post({"ok": True, "ts": "1.2"}) == {"ok": True, "channel": "C123", "ts": "1.2", "already_sent": False}


def test_slack_post_reports_a_gate_block_as_blocked_not_a_crash() -> None:
    verdict = SendVerdict(
        allowed=False, payload="", allowlist_ok=False, reason="slack:C123 is not on the send allowlist"
    )

    assert _post(SendBlockedError(verdict)) == {
        "ok": False,
        "blocked": "slack:C123 is not on the send allowlist",
    }


def test_slack_post_refused_by_slack_is_an_error_never_ok() -> None:
    with pytest.raises(ToolError, match="channel_not_found"):
        _post({"ok": False, "error": "channel_not_found"})


def test_slack_conversations_returns_a_page_and_cursor() -> None:
    result = _call("slack_conversations", {"cursor": "first", "limit": 25})

    assert result.structured_content == {
        "channels": [{"id": "C123"}],
        "next_cursor": "next",
        "cursor": "first",
        "limit": 25,
    }


def test_slack_update_delegates_expected_text_to_egress() -> None:
    with (
        patch.object(services_slack, "_client", return_value=_RefusingBackend()),
        patch.object(services_slack, "OnBehalfSlackEgress") as egress,
    ):
        egress.return_value.update.return_value = {"ok": True}
        result = asyncio.run(
            _server().call_tool("slack_update", {"channel": "C123", "ts": "1.2", "expected_text": "old", "text": "new"})
        )
    assert result.structured_content["ok"] is True
    assert egress.return_value.update.call_args.kwargs == {
        "channel": "C123",
        "ts": "1.2",
        "expected_text": "old",
        "text": "new",
        "thread_ts": "",
    }


def test_slack_delete_defaults_to_preview() -> None:
    with (
        patch.object(services_slack, "_client", return_value=_RefusingBackend()),
        patch.object(services_slack, "OnBehalfSlackEgress") as egress,
    ):
        egress.return_value.delete.return_value = {"ok": True, "dry_run": True, "approval": {"target": "slack:x"}}
        result = asyncio.run(
            _server().call_tool("slack_delete", {"channel": "C123", "ts": "1.2", "expected_text": "old"})
        )
    assert result.structured_content["approval"]["target"] == "slack:x"
    assert egress.return_value.delete.call_args.kwargs["dry_run"] is True


@pytest.mark.parametrize("bot_error", ["channel_not_found", "not_in_channel"])
def test_history_fallback_reaches_the_mcp_caller(bot_error: str) -> None:
    class _Http:
        def __init__(self) -> None:
            self.history_tokens: list[str] = []

        def get(self, method: str, *, token: str, params: dict[str, str | int]) -> RawAPIDict:
            if method == "auth.test":
                return {"ok": False, "error": "unknown"}
            assert method == "conversations.history"
            assert params["channel"] == "D1"
            self.history_tokens.append(token)
            if token == "xoxb-bot":
                return {"ok": False, "error": bot_error}
            return {"ok": True, "messages": [{"ts": "1.0", "text": "hello"}]}

    fake = _Http()
    with patch("teatree.backends.slack.bot.SlackHttpClient", return_value=fake):
        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
    with patch.object(services_slack, "_client", return_value=backend):
        result = asyncio.run(_server().call_tool("slack_channel_history", {"channel": "D1"}))

    assert result.structured_content == {"result": [{"ts": "1.0", "text": "hello", "channel": "D1"}]}
    assert fake.history_tokens == ["xoxb-bot", "xoxp-user"]


def test_history_retry_global_failure_reaches_the_mcp_caller() -> None:
    class _Http:
        def __init__(self) -> None:
            self.history_tokens: list[str] = []

        def get(self, method: str, *, token: str, params: dict[str, str | int]) -> RawAPIDict:
            if method == "auth.test":
                return {"ok": False, "error": "unknown"}
            assert method == "conversations.history"
            assert params["channel"] == "D1"
            self.history_tokens.append(token)
            return {"ok": False, "error": "channel_not_found" if token == "xoxb-bot" else "missing_scope"}

    fake = _Http()
    with patch("teatree.backends.slack.bot.SlackHttpClient", return_value=fake):
        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
    with patch.object(services_slack, "_client", return_value=backend), pytest.raises(ToolError) as refused:
        asyncio.run(_server().call_tool("slack_channel_history", {"channel": "D1"}))

    assert "missing_scope" in str(refused.value)
    assert fake.history_tokens == ["xoxb-bot", "xoxp-user"]


def test_history_description_conditions_token_routing() -> None:
    tools = {tool.name: tool for tool in asyncio.run(_server().list_tools())}
    history = tools["slack_channel_history"].description or ""
    assert "When both tokens are configured" in history
    assert "the user token" in history
    assert "bot-only" in history
    assert "user-only" in history


def test_history_both_token_refusals_reach_the_mcp_caller() -> None:
    class _Http:
        def get(self, method: str, *, token: str, params: dict[str, str | int]) -> RawAPIDict:
            if method == "auth.test":
                return {"ok": False, "error": "unknown"}
            assert method == "conversations.history"
            assert params["channel"] == "D1"
            return {"ok": False, "error": "not_in_channel" if token == "xoxb-bot" else "channel_not_found"}

    with patch("teatree.backends.slack.bot.SlackHttpClient", return_value=_Http()):
        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
    with patch.object(services_slack, "_client", return_value=backend), pytest.raises(ToolError) as refused:
        asyncio.run(_server().call_tool("slack_channel_history", {"channel": "D1"}))

    assert "not_in_channel" in str(refused.value)
    assert "channel_not_found" in str(refused.value)
