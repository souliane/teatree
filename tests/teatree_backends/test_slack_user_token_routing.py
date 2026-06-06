"""Tests for SlackBotBackend's user-token reaction routing.

Slack-Connect externally-shared channels block bot tokens (``xoxb-…``) from
posting reactions with ``mcp_externally_shared_channel_restricted`` /
``not_in_channel``. The fix is to route reactions through the human's
user-OAuth token (``xoxp-…``) when one is configured, while keeping the
bot token as the authoriser for DMs, mentions, and outgoing messages —
DMs are scoped to the bot's IM channels and have to stay on the bot
token.

These tests assert the routing split: ``react`` and ``get_reactions``
authenticate with the user token when present; ``post_message``,
``post_reply``, ``open_dm``, ``fetch_dms``, and ``resolve_user_id`` keep
using the bot token.
"""

from typing import cast

import httpx
import pytest

from teatree.backends.slack import http as slack_http
from teatree.backends.slack.bot import SlackBotBackend


def _capturing_post(captured: list[dict[str, object]]) -> object:
    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        captured.append({"url": url, **kwargs})
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    return fake_post


def _capturing_get(captured: list[dict[str, object]]) -> object:
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        captured.append({"url": url, **kwargs})
        if url.endswith("/conversations.info"):
            # The token-selection policy probes Connect membership here;
            # C0AM3TENTLK is the real Slack-Connect #the-review-crew.
            return httpx.Response(
                200,
                json={"ok": True, "channel": {"is_ext_shared": True}},
                request=httpx.Request("GET", url),
            )
        return httpx.Response(
            200,
            json={"ok": True, "message": {"reactions": [{"name": "eyes"}]}},
            request=httpx.Request("GET", url),
        )

    return fake_get


def _internal_channel_get(url: str, **kwargs: object) -> httpx.Response:
    """Stub ``conversations.info`` as a non-Connect (internal) channel.

    Keeps the post/reply token-routing tests hermetic: without this the
    ``_channel_token`` → ``_is_ext_shared`` → ``_get`` path escapes to a
    real ``slack.com`` GET. ``is_ext_shared: False`` keeps routing on the
    bot token so the ``Bearer xoxb-bot`` assertions hold (the file's
    ``_capturing_get`` forces ``True`` for the Connect-channel reaction
    tests and would flip the asserted token here).
    """
    assert url.endswith("/conversations.info")
    return httpx.Response(
        200,
        json={"ok": True, "channel": {"is_ext_shared": False}},
        request=httpx.Request("GET", url),
    )


class TestReactRoutesThroughUserToken:
    """``react`` uses the user token on Slack-Connect channels (#1072)."""

    def test_react_uses_user_token_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[dict[str, object]] = []
        monkeypatch.setattr(slack_http.httpx, "post", _capturing_post(captured))
        monkeypatch.setattr(slack_http.httpx, "get", _capturing_get(captured))

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.react(channel="C0AM3TENTLK", ts="1779168774.186559", emoji="eyes")

        react_call = next(c for c in captured if c["url"] == "https://slack.com/api/reactions.add")
        headers = cast("dict[str, str]", react_call["headers"])
        assert headers["Authorization"] == "Bearer xoxp-user"

    def test_react_falls_back_to_bot_token_when_user_token_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[dict[str, object]] = []
        monkeypatch.setattr(slack_http.httpx, "post", _capturing_post(captured))

        backend = SlackBotBackend(bot_token="xoxb-bot")
        backend.react(channel="C1", ts="1.0", emoji="eyes")

        headers = cast("dict[str, str]", captured[0]["headers"])
        assert headers["Authorization"] == "Bearer xoxb-bot"

    def test_react_returns_empty_when_neither_token_set(self) -> None:
        backend = SlackBotBackend()
        assert backend.react(channel="C", ts="1.0", emoji="eyes") == {}


class TestGetReactionsRoutesThroughUserToken:
    """``get_reactions`` uses the user token on Slack-Connect channels (#1072)."""

    def test_get_reactions_uses_user_token_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[dict[str, object]] = []
        monkeypatch.setattr(slack_http.httpx, "get", _capturing_get(captured))

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.get_reactions(channel="C0AM3TENTLK", ts="1779168774.186559")

        get_call = next(c for c in captured if c["url"] == "https://slack.com/api/reactions.get")
        headers = cast("dict[str, str]", get_call["headers"])
        assert headers["Authorization"] == "Bearer xoxp-user"

    def test_get_reactions_falls_back_to_bot_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[dict[str, object]] = []
        monkeypatch.setattr(slack_http.httpx, "get", _capturing_get(captured))

        backend = SlackBotBackend(bot_token="xoxb-bot")
        backend.get_reactions(channel="C1", ts="1.0")

        headers = cast("dict[str, str]", captured[0]["headers"])
        assert headers["Authorization"] == "Bearer xoxb-bot"


class TestBotTokenStillAuthorisesNonReactionCalls:
    """DMs, mentions, post_message, open_dm, resolve_user_id keep using bot token.

    The bot token is the only credential authorised for the bot's own DM
    channels, ``conversations.open`` against the bot's IM, and the bot's
    ``users.lookupByEmail`` cache.  Routing those through ``xoxp`` would
    impersonate the user against their own DM history, which the user
    explicitly opted out of by configuring the bot in the first place.
    """

    def test_post_message_uses_bot_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[dict[str, object]] = []
        monkeypatch.setattr(slack_http.httpx, "post", _capturing_post(captured))

        monkeypatch.setattr(slack_http.httpx, "get", _internal_channel_get)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.post_message(channel="C", text="hi")

        headers = cast("dict[str, str]", captured[0]["headers"])
        assert headers["Authorization"] == "Bearer xoxb-bot"
        assert captured[0]["url"] == "https://slack.com/api/chat.postMessage"

    def test_post_reply_uses_bot_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[dict[str, object]] = []
        monkeypatch.setattr(slack_http.httpx, "post", _capturing_post(captured))

        monkeypatch.setattr(slack_http.httpx, "get", _internal_channel_get)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.post_reply(channel="C", ts="1.0", text="hi")

        headers = cast("dict[str, str]", captured[0]["headers"])
        assert headers["Authorization"] == "Bearer xoxb-bot"

    def test_open_dm_uses_bot_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[dict[str, object]] = []

        def fake_post(url: str, **kwargs: object) -> httpx.Response:
            captured.append({"url": url, **kwargs})
            return httpx.Response(
                200,
                json={"ok": True, "channel": {"id": "D1"}},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(slack_http.httpx, "post", fake_post)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.open_dm("U123")

        headers = cast("dict[str, str]", captured[0]["headers"])
        assert headers["Authorization"] == "Bearer xoxb-bot"
        assert captured[0]["url"] == "https://slack.com/api/conversations.open"

    def test_resolve_user_id_uses_bot_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[dict[str, object]] = []

        def fake_get(url: str, **kwargs: object) -> httpx.Response:
            captured.append({"url": url, **kwargs})
            return httpx.Response(
                200,
                json={"ok": True, "members": [{"id": "U1", "name": "alice"}]},
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(slack_http.httpx, "get", fake_get)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.resolve_user_id("alice")

        headers = cast("dict[str, str]", captured[0]["headers"])
        assert headers["Authorization"] == "Bearer xoxb-bot"


class TestUserTokenOnly:
    """A backend configured with only a user token (no bot) still posts reactions."""

    def test_react_works_with_only_user_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[dict[str, object]] = []
        monkeypatch.setattr(slack_http.httpx, "post", _capturing_post(captured))

        backend = SlackBotBackend(user_token="xoxp-user")
        result = backend.react(channel="C", ts="1.0", emoji="eyes")

        assert result == {"ok": True}
        headers = cast("dict[str, str]", captured[0]["headers"])
        assert headers["Authorization"] == "Bearer xoxp-user"

    def test_post_message_uses_user_token_when_only_user_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A user-token-only deployment posts under the user's identity.

        With no bot token there is no second credential to probe Connect
        membership with, and the deployment intends every outbound call
        to go out as the user — the systematic policy routes
        ``chat.postMessage`` through ``xoxp`` (#1072).
        """
        captured: list[dict[str, object]] = []
        monkeypatch.setattr(slack_http.httpx, "post", _capturing_post(captured))

        backend = SlackBotBackend(user_token="xoxp-user")
        result = backend.post_message(channel="C", text="hi")

        assert result == {"ok": True}
        headers = cast("dict[str, str]", captured[0]["headers"])
        assert headers["Authorization"] == "Bearer xoxp-user"
