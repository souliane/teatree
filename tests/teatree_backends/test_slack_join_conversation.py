"""Tests for ``SlackBotBackend.join_conversation`` — bot self-join (#1686)."""

from typing import cast

import httpx
import pytest

from teatree.backends.slack import http as slack_http
from teatree.backends.slack.bot import SlackBotBackend


def _post_returning(body: dict, captured: list[dict[str, object]]) -> object:
    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        captured.append({"url": url, **kwargs})
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    return fake_post


class TestJoinConversation:
    def test_posts_to_conversations_join_with_bot_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[dict[str, object]] = []
        monkeypatch.setattr(slack_http.httpx, "post", _post_returning({"ok": True}, captured))
        backend = SlackBotBackend(bot_token="xoxb-bot")
        body = backend.join_conversation("C123")
        assert body["ok"] is True
        call = captured[0]
        assert cast("str", call["url"]).endswith("/conversations.join")
        assert call["json"] == {"channel": "C123"}
        assert cast("dict[str, str]", call["headers"])["Authorization"] == "Bearer xoxb-bot"

    def test_empty_channel_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called: list[dict[str, object]] = []
        monkeypatch.setattr(slack_http.httpx, "post", _post_returning({"ok": True}, called))
        assert SlackBotBackend(bot_token="xoxb-bot").join_conversation("") == {}
        assert called == []

    def test_returns_error_body_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[dict[str, object]] = []
        failing_post = _post_returning({"ok": False, "error": "missing_scope"}, captured)
        monkeypatch.setattr(slack_http.httpx, "post", failing_post)
        body = SlackBotBackend(bot_token="xoxb-bot").join_conversation("C1")
        assert body["error"] == "missing_scope"
