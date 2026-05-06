"""Tests for MessagingBackend implementations (Noop, Slack)."""

from typing import cast

import httpx
import pytest

from teatree.backends import slack_bot
from teatree.backends.messaging_noop import NoopMessagingBackend
from teatree.backends.protocols import MessagingBackend
from teatree.backends.slack_bot import SlackBotBackend


def test_noop_satisfies_messaging_protocol() -> None:
    assert isinstance(NoopMessagingBackend(), MessagingBackend)


def test_noop_returns_empty_for_inbound() -> None:
    backend = NoopMessagingBackend()
    assert backend.fetch_mentions(since="x") == []
    assert backend.fetch_dms(since="x") == []


def test_noop_returns_empty_dict_for_outbound() -> None:
    backend = NoopMessagingBackend()
    assert backend.post_message(channel="C", text="hi") == {}
    assert backend.post_reply(channel="C", ts="123", text="hi") == {}
    assert backend.react(channel="C", ts="123", emoji="eyes") == {}
    assert backend.resolve_user_id("alice") == ""


def test_slack_satisfies_messaging_protocol() -> None:
    assert isinstance(SlackBotBackend(bot_token="x"), MessagingBackend)


def test_slack_post_message_omits_thread_ts_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, *, headers: dict[str, str], json: dict[str, object], timeout: float) -> httpx.Response:
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return httpx.Response(200, json={"ok": True, "ts": "1.2"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(slack_bot.httpx, "post", fake_post)
    backend = SlackBotBackend(bot_token="xoxb-test")

    result = backend.post_message(channel="C42", text="hello")

    assert result == {"ok": True, "ts": "1.2"}
    assert captured["url"] == "https://slack.com/api/chat.postMessage"
    payload = cast("dict[str, object]", captured["json"])
    assert payload == {"channel": "C42", "text": "hello"}


def test_slack_post_message_includes_thread_ts_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads: list[dict[str, object]] = []

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        payloads.append(cast("dict[str, object]", kwargs["json"]))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(slack_bot.httpx, "post", fake_post)
    backend = SlackBotBackend(bot_token="xoxb-test")

    backend.post_message(channel="C42", text="hello", thread_ts="123.456")

    assert payloads[0] == {"channel": "C42", "text": "hello", "thread_ts": "123.456"}


def test_slack_react_calls_reactions_add(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        captured["url"] = url
        captured["json"] = kwargs["json"]
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(slack_bot.httpx, "post", fake_post)
    backend = SlackBotBackend(bot_token="xoxb-test")

    backend.react(channel="C", ts="1.2", emoji="white_check_mark")

    assert captured["url"] == "https://slack.com/api/reactions.add"
    assert captured["json"] == {"channel": "C", "timestamp": "1.2", "name": "white_check_mark"}


def test_slack_post_returns_empty_when_no_token() -> None:
    backend = SlackBotBackend(bot_token="")
    assert backend.post_message(channel="C", text="hi") == {}
    assert backend.react(channel="C", ts="1", emoji="x") == {}


def test_slack_resolve_user_id_via_lookup_by_email(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        params = kwargs["params"]
        if "lookupByEmail" in url and params == {"email": "alice@example.com"}:
            return httpx.Response(
                200,
                json={"ok": True, "user": {"id": "U999"}},
                request=httpx.Request("GET", url),
            )
        return httpx.Response(200, json={"ok": False}, request=httpx.Request("GET", url))

    monkeypatch.setattr(slack_bot.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test")

    assert backend.resolve_user_id("alice@example.com") == "U999"


def test_slack_resolve_user_id_falls_back_to_user_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        if "users.list" in url:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "members": [
                        {"id": "U1", "name": "bob", "real_name": "Bob"},
                        {"id": "U2", "name": "alice", "real_name": "Alice"},
                    ],
                },
                request=httpx.Request("GET", url),
            )
        return httpx.Response(200, json={"ok": False}, request=httpx.Request("GET", url))

    monkeypatch.setattr(slack_bot.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test")

    assert backend.resolve_user_id("@alice") == "U2"


def test_slack_resolve_user_id_returns_empty_on_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "members": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(slack_bot.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test")

    assert backend.resolve_user_id("ghost") == ""


def test_slack_resolve_user_id_empty_handle_returns_empty() -> None:
    backend = SlackBotBackend(bot_token="xoxb-test")
    assert backend.resolve_user_id("@") == ""


def test_slack_inbound_methods_return_empty_until_socket_mode_enqueues() -> None:
    backend = SlackBotBackend(bot_token="xoxb-test")
    assert backend.fetch_mentions() == []
    assert backend.fetch_dms() == []


def test_slack_fetch_mentions_drains_enqueued_events() -> None:
    backend = SlackBotBackend(bot_token="xoxb-test")
    backend.enqueue_mention({"text": "hi", "ts": "1.0"})
    backend.enqueue_mention({"text": "hello", "ts": "2.0"})

    first = backend.fetch_mentions()
    second = backend.fetch_mentions()

    assert [e["ts"] for e in first] == ["1.0", "2.0"]
    assert second == []


def test_slack_fetch_dms_drains_enqueued_events() -> None:
    backend = SlackBotBackend(bot_token="xoxb-test")
    backend.enqueue_dm({"text": "dm", "ts": "3.0"})

    drained = backend.fetch_dms()

    assert [e["ts"] for e in drained] == ["3.0"]
    assert backend.fetch_dms() == []


def test_slack_exposes_app_token_and_user_id() -> None:
    backend = SlackBotBackend(bot_token="xoxb", app_token="xapp", user_id="U123")
    assert backend.app_token == "xapp"
    assert backend.user_id == "U123"


def test_slack_open_dm_returns_channel_id(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "channel": {"id": "D9XYZ"}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(slack_bot.httpx, "post", fake_post)
    backend = SlackBotBackend(bot_token="xoxb-test")

    assert backend.open_dm("U01ABCD1234") == "D9XYZ"


def test_slack_open_dm_returns_empty_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json={"ok": False}, request=httpx.Request("POST", url))

    monkeypatch.setattr(slack_bot.httpx, "post", fake_post)
    backend = SlackBotBackend(bot_token="xoxb-test")

    assert backend.open_dm("U01ABCD1234") == ""


def test_slack_get_reactions_returns_emoji_names(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "message": {
                    "reactions": [
                        {"name": "white_check_mark", "users": ["U1"], "count": 1},
                        {"name": "eyes", "users": ["U2"], "count": 1},
                    ],
                },
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(slack_bot.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test")

    assert backend.get_reactions(channel="C1", ts="123.456") == ["white_check_mark", "eyes"]


def test_slack_get_reactions_returns_empty_when_no_reactions(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "message": {}},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(slack_bot.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test")

    assert backend.get_reactions(channel="C1", ts="123.456") == []
