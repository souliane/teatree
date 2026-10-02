"""The Slack thread-replies MCP tool fails loud when Slack refuses the read.

A bot token reads only channels the bot was invited to; Slack answers the rest with
``not_in_channel``, and an empty list for that reports "nobody replied" when the truth
is "I could not look". The read is retried through the user token, and a refusal
neither token can get past surfaces with Slack's own error code.
"""

import asyncio
from typing import cast
from unittest.mock import patch

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from teatree.backends.slack import http as slack_http
from teatree.backends.slack.bot import SlackBotBackend
from teatree.backends.slack.bot_errors import SlackReadRefusedError
from teatree.mcp.services_slack import _slack_thread_replies
from teatree.types import RawAPIDict

CHANNEL = "C_TEAM"
THREAD = "1700000000.000100"
BOT_NOT_IN_CHANNEL: RawAPIDict = {"ok": False, "error": "not_in_channel"}


def _serve(monkeypatch: pytest.MonkeyPatch, by_token: dict[str, RawAPIDict]) -> None:
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        assert url.endswith("/conversations.replies")
        bearer = cast("dict[str, str]", kwargs["headers"])["Authorization"].removeprefix("Bearer ")
        return httpx.Response(200, json=by_token[bearer], request=httpx.Request("GET", url))

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        _ = kwargs
        return httpx.Response(200, json={"ok": True, "user_id": "UBOT"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(slack_http.httpx, "get", fake_get)
    monkeypatch.setattr(slack_http.httpx, "post", fake_post)


def _call() -> list[RawAPIDict]:
    backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
    with patch("teatree.mcp.services_slack._client", return_value=backend):
        return asyncio.run(_slack_thread_replies(CHANNEL, THREAD))


def test_a_thread_the_bot_is_not_in_is_read_through_the_user_token(monkeypatch: pytest.MonkeyPatch) -> None:
    thread = [{"ts": THREAD, "text": "root"}, *({"ts": f"1700000001.00000{i}", "text": f"r{i}"} for i in range(7))]
    _serve(monkeypatch, {"xoxb-bot": BOT_NOT_IN_CHANNEL, "xoxp-user": {"ok": True, "messages": thread}})

    replies = _call()

    assert [r["text"] for r in replies] == ["root", "r0", "r1", "r2", "r3", "r4", "r5", "r6"]


def test_a_thread_neither_token_can_read_fails_loud_with_the_slack_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, {"xoxb-bot": BOT_NOT_IN_CHANNEL, "xoxp-user": {"ok": False, "error": "channel_not_found"}})

    with pytest.raises(SlackReadRefusedError, match="channel_not_found"):
        _call()


def test_a_genuinely_empty_thread_still_answers_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, {"xoxb-bot": {"ok": True, "messages": []}})

    assert _call() == []


@pytest.mark.parametrize(("channel", "thread_ts"), [("", THREAD), (CHANNEL, "")])
def test_empty_thread_coordinates_are_refused_never_read_as_an_empty_thread(channel: str, thread_ts: str) -> None:
    with (
        patch("teatree.mcp.services_slack._client", return_value=SlackBotBackend(bot_token="xoxb-bot")),
        pytest.raises(ToolError, match="non-empty channel and thread_ts"),
    ):
        asyncio.run(_slack_thread_replies(channel, thread_ts))
