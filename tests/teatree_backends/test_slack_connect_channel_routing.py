"""Tests for the systematic Slack-Connect token-selection policy.

Slack-Connect externally-shared channels (e.g. a shared partner channel)
reject the bot token with
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

from teatree.backends.slack import bot as slack_bot
from teatree.backends.slack import http as slack_http
from teatree.backends.slack.bot import SlackBotBackend

_EXT_SHARED = "C0DEMOCHAN1"  # an externally-shared partner channel (Slack-Connect)
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
        monkeypatch.setattr(slack_http.httpx, "post", fake_post)
        monkeypatch.setattr(slack_http.httpx, "get", fake_get)

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
        monkeypatch.setattr(slack_http.httpx, "post", fake_post)
        monkeypatch.setattr(slack_http.httpx, "get", fake_get)

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
        monkeypatch.setattr(slack_http.httpx, "post", fake_post)
        monkeypatch.setattr(slack_http.httpx, "get", fake_get)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.post_message(channel=_INTERNAL, text="status")

        assert _auth_header_for(captured, url_suffix="/chat.postMessage") == "Bearer xoxb-bot"

    def test_post_message_to_dm_skips_lookup_and_uses_bot_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[dict[str, object]] = []
        fake_post, fake_get = _router(captured, ext_shared_channels=set())
        monkeypatch.setattr(slack_http.httpx, "post", fake_post)
        monkeypatch.setattr(slack_http.httpx, "get", fake_get)

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
        monkeypatch.setattr(slack_http.httpx, "post", fake_post)
        monkeypatch.setattr(slack_http.httpx, "get", fake_get)

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
        monkeypatch.setattr(slack_http.httpx, "post", fake_post)
        monkeypatch.setattr(slack_http.httpx, "get", fake_get)

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
        monkeypatch.setattr(slack_http.httpx, "post", fake_post)
        monkeypatch.setattr(slack_http.httpx, "get", fake_get)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.react(channel=_INTERNAL, ts="1.0", emoji="eyes")

        assert _auth_header_for(captured, url_suffix="/reactions.add") == "Bearer xoxb-bot"

    def test_react_on_ext_shared_channel_uses_user_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[dict[str, object]] = []
        fake_post, fake_get = _router(captured, ext_shared_channels={_EXT_SHARED})
        monkeypatch.setattr(slack_http.httpx, "post", fake_post)
        monkeypatch.setattr(slack_http.httpx, "get", fake_get)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.react(channel=_EXT_SHARED, ts="1.0", emoji="eyes")

        assert _auth_header_for(captured, url_suffix="/reactions.add") == "Bearer xoxp-user"

    def test_get_reactions_on_ext_shared_channel_uses_user_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[dict[str, object]] = []
        fake_post, fake_get = _router(captured, ext_shared_channels={_EXT_SHARED})
        monkeypatch.setattr(slack_http.httpx, "post", fake_post)
        monkeypatch.setattr(slack_http.httpx, "get", fake_get)

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
        monkeypatch.setattr(slack_http.httpx, "post", fake_post)
        monkeypatch.setattr(slack_http.httpx, "get", fake_get)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        assert backend._channel_token(_EXT_SHARED, op=slack_bot.SlackOp.WRITE) == "xoxp-user"
        assert backend._channel_token(_INTERNAL, op=slack_bot.SlackOp.WRITE) == "xoxb-bot"
        assert backend._channel_token(_DM, op=slack_bot.SlackOp.WRITE) == "xoxb-bot"

    def test_channel_token_write_fails_toward_user_on_info_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1110 behaviour change: a WRITE in an ambiguous channel -> xoxp.

        Pre-#1110 this asserted ``== "xoxb-bot"`` (the buggy fail-safe):
        ``conversations.info`` ``ok:false`` was treated as
        "internal" and the post went out under the bot token, which
        Slack-Connect rejects with
        ``mcp_externally_shared_channel_restricted`` — silently dropping
        the partner write. The intentional new contract: when Connect
        membership cannot be confirmed, a WRITE fails *toward* the user
        ``xoxp`` token (the only token that can reach a Connect channel),
        never silently toward the bot. This is the central anti-vacuous
        proof for #1110.
        """
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

        monkeypatch.setattr(slack_http.httpx, "get", fake_get)
        monkeypatch.setattr(slack_http.httpx, "post", fake_post)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        assert backend._channel_token(_EXT_SHARED, op=slack_bot.SlackOp.WRITE) == "xoxp-user"

    def test_channel_token_read_uses_user_token_and_skips_the_probe(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A genuine READ routes to the user token and never probes membership.

        The pre-flip policy routed an ambiguous READ to the bot token
        (asserted ``== "xoxb-bot"`` here). The new intent: a general
        viewing read goes out under the user ``xoxp`` on any non-DM
        channel so the agent sees what the user sees — and because a READ
        never consults ``is_ext_shared``, the bot ``conversations.info``
        probe is skipped entirely (no GET is issued).
        """
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

        monkeypatch.setattr(slack_http.httpx, "get", fake_get)
        monkeypatch.setattr(slack_http.httpx, "post", fake_post)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        assert backend._channel_token(_EXT_SHARED, op=slack_bot.SlackOp.READ) == "xoxp-user"
        assert captured == []  # no conversations.info probe for a READ

    def test_react_on_info_failed_channel_uses_user_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A reaction on an ambiguous channel goes out under xoxp (#1110).

        Reacting is a WRITE. Pre-#1110 a flaky ``conversations.info``
        sent ``reactions.add`` under the bot token (RED here: xoxb),
        which Slack-Connect rejects — the agent's ``:eyes:`` ack on a
        partner review channel silently never lands.
        """
        captured: list[dict[str, object]] = []

        def fake_get(url: str, **kwargs: object) -> httpx.Response:
            captured.append({"method": "GET", "url": url, **kwargs})
            return httpx.Response(
                200,
                json={"ok": False, "error": "ratelimited"},
                request=httpx.Request("GET", url),
            )

        def fake_post(url: str, **kwargs: object) -> httpx.Response:
            captured.append({"method": "POST", "url": url, **kwargs})
            return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

        monkeypatch.setattr(slack_http.httpx, "get", fake_get)
        monkeypatch.setattr(slack_http.httpx, "post", fake_post)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.react(channel=_EXT_SHARED, ts="1.0", emoji="eyes")

        assert _auth_header_for(captured, url_suffix="/reactions.add") == "Bearer xoxp-user"

    def test_post_message_internal_still_bot(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A confirmed-internal channel post is bit-for-bit unchanged.

        ``conversations.info`` returns ``ok:true`` /
        ``is_ext_shared:false`` -> the policy resolves a concrete
        ``False`` (not the ambiguous ``None``) and keeps the bot token.
        """
        captured: list[dict[str, object]] = []
        fake_post, fake_get = _router(captured, ext_shared_channels=set())
        monkeypatch.setattr(slack_http.httpx, "post", fake_post)
        monkeypatch.setattr(slack_http.httpx, "get", fake_get)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.post_message(channel=_INTERNAL, text="status")

        assert _auth_header_for(captured, url_suffix="/chat.postMessage") == "Bearer xoxb-bot"

    def test_dm_skips_probe_and_uses_bot(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The 131-row DM drain path: ``D…`` -> bot, no probe (regression pin).

        DMs short-circuit to the bot token *before* any Connect probe.
        The 2026-05-19 131-row DM drain worked precisely because
        ``channel.startswith("D")`` -> bot; #1110's ambiguous-WRITE
        branch must never regress this.
        """
        captured: list[dict[str, object]] = []
        fake_post, fake_get = _router(captured, ext_shared_channels=set())
        monkeypatch.setattr(slack_http.httpx, "post", fake_post)
        monkeypatch.setattr(slack_http.httpx, "get", fake_get)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.post_message(channel=_DM, text="dm drain")

        assert _auth_header_for(captured, url_suffix="/chat.postMessage") == "Bearer xoxb-bot"
        assert not any(str(c["url"]).endswith("/conversations.info") for c in captured)

    def test_repeat_posts_after_info_failure_reprobe(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unknown (``None``) membership is NOT cached — re-probed next call.

        Pre-#1110 an ``ok:false`` was cached as ``False`` (so a later
        recovered ``conversations.info`` was never consulted). #1110
        caches only concrete ``True`` / ``False``; an unknown re-probes,
        so a transient failure that recovers resolves correctly on the
        second post. RED on main: there is no ``None`` concept and the
        ``False`` is cached, so the second call issues no probe.
        """
        captured: list[dict[str, object]] = []
        responses = iter(
            [
                {"ok": False, "error": "ratelimited"},
                {"ok": True, "channel": {"id": _EXT_SHARED, "is_ext_shared": True, "is_shared": True}},
            ]
        )

        def fake_get(url: str, **kwargs: object) -> httpx.Response:
            captured.append({"method": "GET", "url": url, **kwargs})
            if url.endswith("/conversations.info"):
                return httpx.Response(200, json=next(responses), request=httpx.Request("GET", url))
            return httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", url))

        def fake_post(url: str, **kwargs: object) -> httpx.Response:
            captured.append({"method": "POST", "url": url, **kwargs})
            return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

        monkeypatch.setattr(slack_http.httpx, "get", fake_get)
        monkeypatch.setattr(slack_http.httpx, "post", fake_post)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.post_message(channel=_EXT_SHARED, text="one")  # info fails -> WRITE -> xoxp
        backend.post_message(channel=_EXT_SHARED, text="two")  # re-probe -> confirmed ext-shared -> xoxp

        info_calls = [c for c in captured if str(c["url"]).endswith("/conversations.info")]
        assert len(info_calls) == 2  # None was NOT cached; second post re-probes
        post_calls = [c for c in captured if str(c["url"]).endswith("/chat.postMessage")]
        for call in post_calls:
            headers = cast("dict[str, str]", call["headers"])
            assert headers["Authorization"] == "Bearer xoxp-user"


class TestFetchChannelHistoryRoutedAsTheBroadcastPost:
    """``fetch_channel_history`` reads under the token its reaction post will use.

    The broadcast scanner reads a Slack-Connect review channel to find MR
    URLs and then reacts on those messages. A bot-token history read on a
    Connect channel returns empty (``mcp_externally_shared_channel_restricted``)
    and silently drops every broadcast — so ``fetch_channel_history`` must
    consult the same WRITE-class token resolution ``post_message`` /
    ``react`` does. read-token == post-token is the load-bearing
    invariant from #1084 carried into the #1255 history-read path.
    """

    def test_history_on_ext_shared_channel_uses_user_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[dict[str, object]] = []
        fake_post, fake_get = _router(captured, ext_shared_channels={_EXT_SHARED})
        monkeypatch.setattr(slack_http.httpx, "post", fake_post)
        monkeypatch.setattr(slack_http.httpx, "get", fake_get)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.fetch_channel_history(channel=_EXT_SHARED, limit=10)

        assert _auth_header_for(captured, url_suffix="/conversations.history") == "Bearer xoxp-user"

    def test_history_on_info_failed_channel_uses_user_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ambiguous membership (bot can't see the Connect channel) -> xoxp.

        Realistic Slack-Connect deployment failure mode: the bot has no
        access to the Connect channel at all, so its
        ``conversations.info`` call also fails. Routing under WRITE
        semantics makes the ambiguous branch fall toward xoxp — the only
        token that can reach the channel — so the broadcast scanner
        actually reads messages instead of silently seeing an empty
        history every tick.
        """
        captured: list[dict[str, object]] = []

        def fake_get(url: str, **kwargs: object) -> httpx.Response:
            captured.append({"method": "GET", "url": url, **kwargs})
            return httpx.Response(
                200,
                json={"ok": False, "error": "channel_not_found"},
                request=httpx.Request("GET", url),
            )

        def fake_post(url: str, **kwargs: object) -> httpx.Response:
            return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

        monkeypatch.setattr(slack_http.httpx, "get", fake_get)
        monkeypatch.setattr(slack_http.httpx, "post", fake_post)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.fetch_channel_history(channel=_EXT_SHARED, limit=10)

        assert _auth_header_for(captured, url_suffix="/conversations.history") == "Bearer xoxp-user"

    def test_history_on_internal_channel_uses_bot_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A confirmed-internal channel history read stays on bot.

        Regression pin: the policy must not over-route to xoxp on
        ordinary internal channels just because we lifted READ -> WRITE
        for the history path.
        """
        captured: list[dict[str, object]] = []
        fake_post, fake_get = _router(captured, ext_shared_channels=set())
        monkeypatch.setattr(slack_http.httpx, "post", fake_post)
        monkeypatch.setattr(slack_http.httpx, "get", fake_get)

        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        backend.fetch_channel_history(channel=_INTERNAL, limit=10)

        assert _auth_header_for(captured, url_suffix="/conversations.history") == "Bearer xoxb-bot"
