"""Tests for the systematic Slack-Connect token-selection policy.

Slack-Connect externally-shared channels (``#the-review-team``,
``#client-term-redacted``) reject the bot token with
``mcp_externally_shared_channel_restricted``. The user's personal
``xoxp-…`` token *is* a member of those partner channels and can post
there. The deterministic policy: ``chat.postMessage`` (and reactions) to
an externally-shared / Slack-Connect channel routes through the user
token; ordinary internal channels and DMs keep the bot token.

Connect membership is detected deterministically via
``conversations.info`` (``is_ext_shared`` / ``is_shared``), cached per
channel id — not by a try-bot-then-fallback error probe. The selection
is concentrated in one function, :meth:`SlackBotBackend._channel_token`,
which every outbound surface consults.
"""

from typing import cast

import httpx
import pytest

from teatree.backends import slack_bot
from teatree.backends.slack_bot import SlackBotBackend

_EXT_SHARED = "C0DEMOCHAN1"  # #the-review-team (Slack-Connect)
_INTERNAL = "C09INTERNAL0"  # an ordinary workspace channel
_DM = "D0000000001"


def _conversations_info_response(*, is_ext_shared: bool) -> dict[str, object]:
    return {
        "ok": True,
        "channel": {
            "id": _EXT_SHARED if is_ext_shared else _INTERNAL,
            "is_ext_shared": is_ext_shared,
            "is_shared": is_ext_shared,
        },
    }


def _router(
    captured: list[dict[str, object]],
    *,
    ext_shared_channels: set[str],
) -> tuple[object, object]:
    """Return ``(fake_post, fake_get)`` that record every call.

    ``conversations.info`` answers ``is_ext_shared`` based on the
    requested channel so the policy can resolve membership
    deterministically; all other calls return a bland ``{"ok": True}``.
    """

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        captured.append({"method": "GET", "url": url, **kwargs})
        if url.endswith("/conversations.info"):
            params = cast("dict[str, object]", kwargs.get("params") or {})
            channel = str(params.get("channel", ""))
            return httpx.Response(
                200,
                json=_conversations_info_response(is_ext_shared=channel in ext_shared_channels),
                request=httpx.Request("GET", url),
            )
        return httpx.Response(
            200,
            json={"ok": True, "message": {"reactions": [{"name": "eyes"}]}},
            request=httpx.Request("GET", url),
        )

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        captured.append({"method": "POST", "url": url, **kwargs})
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    return fake_post, fake_get


def _auth_header_for(captured: list[dict[str, object]], *, url_suffix: str) -> str:
    for call in captured:
        if str(call["url"]).endswith(url_suffix):
            headers = cast("dict[str, str]", call["headers"])
            return headers["Authorization"]
    message = f"no call to {url_suffix} in {[c['url'] for c in captured]}"
    raise AssertionError(message)


class TestPostMessageRoutesConnectChannelsThroughUserToken:
    """``chat.postMessage`` to an ext-shared channel uses the xoxp token."""

    def test_post_message_to_ext_shared_channel_uses_user_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[dict[str, object]] = []
        fake_post, fake_get = _router(captured, ext_shared_channels={_EXT_SHARED})
        monkeypatch.setattr(slack_bot.httpx, "post", fake_post)
        monkeypatch.setattr(slack_bot.httpx, "get", fake_get)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        result = backend.post_message(channel=_EXT_SHARED, text="review please")

        assert result == {"ok": True}
        # conversations.info is the bot's own scoped call — bot token.
        assert _auth_header_for(captured, url_suffix="/conversations.info") == "Bearer xoxb-bot"
        # the actual post must go out under the user's identity.
        assert _auth_header_for(captured, url_suffix="/chat.postMessage") == "Bearer xoxp-user"

    def test_post_reply_to_ext_shared_channel_uses_user_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[dict[str, object]] = []
        fake_post, fake_get = _router(captured, ext_shared_channels={_EXT_SHARED})
        monkeypatch.setattr(slack_bot.httpx, "post", fake_post)
        monkeypatch.setattr(slack_bot.httpx, "get", fake_get)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.post_reply(channel=_EXT_SHARED, ts="1.0", text="ping")

        assert _auth_header_for(captured, url_suffix="/chat.postMessage") == "Bearer xoxp-user"


class TestPostMessageKeepsInternalChannelsOnBotToken:
    """Ordinary internal channels and DMs keep the bot token."""

    def test_post_message_to_internal_channel_uses_bot_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[dict[str, object]] = []
        fake_post, fake_get = _router(captured, ext_shared_channels=set())
        monkeypatch.setattr(slack_bot.httpx, "post", fake_post)
        monkeypatch.setattr(slack_bot.httpx, "get", fake_get)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.post_message(channel=_INTERNAL, text="status")

        assert _auth_header_for(captured, url_suffix="/chat.postMessage") == "Bearer xoxb-bot"

    def test_post_message_to_dm_skips_lookup_and_uses_bot_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[dict[str, object]] = []
        fake_post, fake_get = _router(captured, ext_shared_channels=set())
        monkeypatch.setattr(slack_bot.httpx, "post", fake_post)
        monkeypatch.setattr(slack_bot.httpx, "get", fake_get)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.post_message(channel=_DM, text="dm")

        assert _auth_header_for(captured, url_suffix="/chat.postMessage") == "Bearer xoxb-bot"
        # DM channels never trigger a Connect-membership probe.
        assert not any(str(c["url"]).endswith("/conversations.info") for c in captured)

    def test_no_user_token_keeps_everything_on_bot_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[dict[str, object]] = []
        fake_post, fake_get = _router(captured, ext_shared_channels={_EXT_SHARED})
        monkeypatch.setattr(slack_bot.httpx, "post", fake_post)
        monkeypatch.setattr(slack_bot.httpx, "get", fake_get)

        backend = SlackBotBackend(bot_token="xoxb-bot")
        backend.post_message(channel=_EXT_SHARED, text="hi")

        assert _auth_header_for(captured, url_suffix="/chat.postMessage") == "Bearer xoxb-bot"
        # With no user token there is nothing to route to — skip the probe.
        assert not any(str(c["url"]).endswith("/conversations.info") for c in captured)


class TestConnectMembershipIsCachedPerChannel:
    """``conversations.info`` is called once per channel id, then cached."""

    def test_repeat_posts_to_same_channel_probe_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[dict[str, object]] = []
        fake_post, fake_get = _router(captured, ext_shared_channels={_EXT_SHARED})
        monkeypatch.setattr(slack_bot.httpx, "post", fake_post)
        monkeypatch.setattr(slack_bot.httpx, "get", fake_get)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.post_message(channel=_EXT_SHARED, text="one")
        backend.post_message(channel=_EXT_SHARED, text="two")

        info_calls = [c for c in captured if str(c["url"]).endswith("/conversations.info")]
        assert len(info_calls) == 1
        post_calls = [c for c in captured if str(c["url"]).endswith("/chat.postMessage")]
        assert len(post_calls) == 2
        for call in post_calls:
            headers = cast("dict[str, str]", call["headers"])
            assert headers["Authorization"] == "Bearer xoxp-user"


class TestReactionsRoutedByTheSamePolicy:
    """Reactions consult the same per-channel policy (fixes #1072).

    The pre-#1072 ``_reaction_token`` routed *every* reaction through
    xoxp whenever it was configured, even on internal/DM channels where
    the bot token already has ``reactions:write``. The systematic policy
    routes only ext-shared channels through xoxp.
    """

    def test_react_on_internal_channel_uses_bot_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[dict[str, object]] = []
        fake_post, fake_get = _router(captured, ext_shared_channels=set())
        monkeypatch.setattr(slack_bot.httpx, "post", fake_post)
        monkeypatch.setattr(slack_bot.httpx, "get", fake_get)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.react(channel=_INTERNAL, ts="1.0", emoji="eyes")

        assert _auth_header_for(captured, url_suffix="/reactions.add") == "Bearer xoxb-bot"

    def test_react_on_ext_shared_channel_uses_user_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[dict[str, object]] = []
        fake_post, fake_get = _router(captured, ext_shared_channels={_EXT_SHARED})
        monkeypatch.setattr(slack_bot.httpx, "post", fake_post)
        monkeypatch.setattr(slack_bot.httpx, "get", fake_get)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.react(channel=_EXT_SHARED, ts="1.0", emoji="eyes")

        assert _auth_header_for(captured, url_suffix="/reactions.add") == "Bearer xoxp-user"

    def test_get_reactions_on_ext_shared_channel_uses_user_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[dict[str, object]] = []
        fake_post, fake_get = _router(captured, ext_shared_channels={_EXT_SHARED})
        monkeypatch.setattr(slack_bot.httpx, "post", fake_post)
        monkeypatch.setattr(slack_bot.httpx, "get", fake_get)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.get_reactions(channel=_EXT_SHARED, ts="1.0")

        assert _auth_header_for(captured, url_suffix="/reactions.get") == "Bearer xoxp-user"


class TestChannelTokenIsTheSingleSelectionPoint:
    """The selection is one well-tested function, not scattered conditionals."""

    def test_channel_token_returns_user_token_for_ext_shared(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[dict[str, object]] = []
        fake_post, fake_get = _router(captured, ext_shared_channels={_EXT_SHARED})
        monkeypatch.setattr(slack_bot.httpx, "post", fake_post)
        monkeypatch.setattr(slack_bot.httpx, "get", fake_get)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        assert backend._channel_token(_EXT_SHARED) == "xoxp-user"
        assert backend._channel_token(_INTERNAL) == "xoxb-bot"
        assert backend._channel_token(_DM) == "xoxb-bot"

    def test_channel_token_fails_safe_to_bot_on_info_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[dict[str, object]] = []

        def fake_get(url: str, **kwargs: object) -> httpx.Response:
            captured.append({"url": url, **kwargs})
            return httpx.Response(
                200,
                json={"ok": False, "error": "channel_not_found"},
                request=httpx.Request("GET", url),
            )

        def fake_post(url: str, **kwargs: object) -> httpx.Response:
            return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

        monkeypatch.setattr(slack_bot.httpx, "get", fake_get)
        monkeypatch.setattr(slack_bot.httpx, "post", fake_post)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        assert backend._channel_token(_EXT_SHARED) == "xoxb-bot"
