"""Tests for MessagingBackend implementations (Noop, Slack)."""

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import cast

import httpx
import pytest

from teatree.backends import slack_http, slack_scopes
from teatree.backends.messaging_noop import NoopMessagingBackend
from teatree.backends.slack_bot import SlackBotBackend
from teatree.core.backend_protocols import MessagingBackend


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
    assert backend.post_routed(channel="C", text="hi") == {}
    assert backend.react_routed(channel="C", ts="123", emoji="eyes") == {}
    assert backend.resolve_user_id("alice") == ""
    assert backend.post_audio_dm(channel="C", filepath="/tmp/x.m4a", text="hi", title="t") == {}


def test_slack_satisfies_messaging_protocol() -> None:
    assert isinstance(SlackBotBackend(bot_token="xoxb-x"), MessagingBackend)


def test_slack_post_message_omits_thread_ts_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, *, headers: dict[str, str], json: dict[str, object], timeout: float) -> httpx.Response:
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return httpx.Response(200, json={"ok": True, "ts": "1.2"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(slack_http.httpx, "post", fake_post)
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

    monkeypatch.setattr(slack_http.httpx, "post", fake_post)
    backend = SlackBotBackend(bot_token="xoxb-test")

    backend.post_message(channel="C42", text="hello", thread_ts="123.456")

    assert payloads[0] == {"channel": "C42", "text": "hello", "thread_ts": "123.456"}


def test_slack_react_calls_reactions_add(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        captured["url"] = url
        captured["json"] = kwargs["json"]
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(slack_http.httpx, "post", fake_post)
    backend = SlackBotBackend(bot_token="xoxb-test")

    backend.react(channel="C", ts="1.2", emoji="white_check_mark")

    assert captured["url"] == "https://slack.com/api/reactions.add"
    assert captured["json"] == {"channel": "C", "timestamp": "1.2", "name": "white_check_mark"}


def test_slack_auth_test_returns_raw_response(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        captured["url"] = url
        return httpx.Response(
            200,
            json={"ok": True, "user_id": "UBOT", "team": "T1"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(slack_http.httpx, "post", fake_post)
    backend = SlackBotBackend(bot_token="xoxb-test")

    result = backend.auth_test()

    assert result["ok"] is True
    assert result["user_id"] == "UBOT"
    assert result["team"] == "T1"
    assert captured["url"] == "https://slack.com/api/auth.test"


def test_slack_auth_test_surfaces_ok_false(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": False, "error": "missing_scope"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(slack_http.httpx, "post", fake_post)
    backend = SlackBotBackend(bot_token="xoxb-test")

    result = backend.auth_test()
    assert result["ok"] is False
    assert result["error"] == "missing_scope"


def test_slack_auth_test_returns_empty_when_no_token() -> None:
    assert SlackBotBackend(bot_token="").auth_test() == {}


def test_slack_auth_test_surfaces_granted_scopes_from_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slack reports granted OAuth scopes in the ``X-OAuth-Scopes`` *header*, not the JSON body.

    A scope guard built on ``auth_test()`` is dead in production unless the
    backend surfaces what the header carries. This test puts the scope ONLY in
    the header (never a fabricated JSON ``scopes`` field) and asserts it is
    surfaced under ``GRANTED_SCOPES_KEY``.
    """

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"X-OAuth-Scopes": "chat:write,reactions:write,users:read"},
            json={"ok": True, "user_id": "UBOT", "bot_id": "BBOT"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(slack_http.httpx, "post", fake_post)
    backend = SlackBotBackend(bot_token="xoxb-test")

    result = backend.auth_test()

    assert result["ok"] is True
    assert result["user_id"] == "UBOT"
    assert result[slack_scopes.GRANTED_SCOPES_KEY] == ["chat:write", "reactions:write", "users:read"]


def test_slack_auth_test_surfaces_empty_scopes_when_header_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "user_id": "UBOT"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(slack_http.httpx, "post", fake_post)
    backend = SlackBotBackend(bot_token="xoxb-test")

    assert backend.auth_test()[slack_scopes.GRANTED_SCOPES_KEY] == []


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

    monkeypatch.setattr(slack_http.httpx, "get", fake_get)
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

    monkeypatch.setattr(slack_http.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test")

    assert backend.resolve_user_id("@alice") == "U2"


def test_slack_resolve_user_id_returns_empty_on_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "members": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(slack_http.httpx, "get", fake_get)
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
    backend.inbound.enqueue_mention({"text": "hi", "ts": "1.0"})
    backend.inbound.enqueue_mention({"text": "hello", "ts": "2.0"})

    first = backend.fetch_mentions()
    second = backend.fetch_mentions()

    assert [e["ts"] for e in first] == ["1.0", "2.0"]
    assert [e["ts"] for e in second] == ["1.0", "2.0"]


def test_slack_fetch_dms_drains_enqueued_events() -> None:
    backend = SlackBotBackend(bot_token="xoxb-test")
    backend.inbound.enqueue_dm({"text": "dm", "ts": "3.0"})

    drained = backend.fetch_dms()

    assert [e["ts"] for e in drained] == ["3.0"]
    assert [e["ts"] for e in backend.fetch_dms()] == ["3.0"]


def test_slack_fetch_dms_non_destructive_across_scanners_in_tick() -> None:
    backend = SlackBotBackend(bot_token="")
    backend.inbound.enqueue_dm({"text": "red card", "ts": "9.0", "channel": "D1"})

    slack_dm_inbound = backend.fetch_dms()
    slack_mentions = backend.fetch_dms()
    red_card = backend.fetch_dms()

    assert [e["ts"] for e in slack_dm_inbound] == ["9.0"]
    assert [e["ts"] for e in slack_mentions] == ["9.0"]
    assert [e["ts"] for e in red_card] == ["9.0"]


def test_slack_fetch_reactions_non_destructive_across_scanners_in_tick() -> None:
    backend = SlackBotBackend(bot_token="")
    backend.inbound.enqueue_reaction({"reaction": "no_entry_sign", "event_ts": "9.1"})

    review_intent = backend.fetch_reactions()
    red_card = backend.fetch_reactions()

    assert [e["event_ts"] for e in review_intent] == ["9.1"]
    assert [e["event_ts"] for e in red_card] == ["9.1"]


def test_slack_fetch_mentions_non_destructive_across_scanners_in_tick() -> None:
    backend = SlackBotBackend(bot_token="")
    backend.inbound.enqueue_mention({"text": "@bot", "ts": "9.2"})

    mentions_scanner = backend.fetch_mentions()
    review_intent = backend.fetch_mentions()

    assert [e["ts"] for e in mentions_scanner] == ["9.2"]
    assert [e["ts"] for e in review_intent] == ["9.2"]


def test_slack_fetch_dms_concurrent_consumers_each_see_event() -> None:
    backend = SlackBotBackend(bot_token="")
    backend.inbound.enqueue_dm({"text": "dm", "ts": "9.3"})

    barrier = threading.Barrier(3)

    def consume() -> list[str]:
        barrier.wait()
        return [str(e["ts"]) for e in backend.fetch_dms()]

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: consume(), range(3)))

    assert all(r == ["9.3"] for r in results)


def test_slack_fetch_dms_new_enqueue_rolls_batch() -> None:
    backend = SlackBotBackend(bot_token="")
    backend.inbound.enqueue_dm({"text": "first tick", "ts": "10.0"})

    assert [e["ts"] for e in backend.fetch_dms()] == ["10.0"]

    backend.inbound.enqueue_dm({"text": "second tick", "ts": "11.0"})

    assert [e["ts"] for e in backend.fetch_dms()] == ["11.0"]
    assert [e["ts"] for e in backend.fetch_dms()] == ["11.0"]


def test_slack_exposes_app_token_and_user_id() -> None:
    backend = SlackBotBackend(bot_token="xoxb-test", app_token="xapp-test", user_id="U123")
    assert backend.app_token == "xapp-test"
    assert backend.user_id == "U123"


def test_slack_open_dm_returns_channel_id(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "channel": {"id": "D9XYZ"}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(slack_http.httpx, "post", fake_post)
    backend = SlackBotBackend(bot_token="xoxb-test")

    assert backend.open_dm("U01ABCD1234") == "D9XYZ"


def test_slack_open_dm_returns_empty_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json={"ok": False}, request=httpx.Request("POST", url))

    monkeypatch.setattr(slack_http.httpx, "post", fake_post)
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

    monkeypatch.setattr(slack_http.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test")

    assert backend.get_reactions(channel="C1", ts="123.456") == ["white_check_mark", "eyes"]


def test_slack_get_reactions_returns_empty_when_no_reactions(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "message": {}},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(slack_http.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test")

    assert backend.get_reactions(channel="C1", ts="123.456") == []


def test_slack_get_returns_empty_when_no_token() -> None:
    backend = SlackBotBackend(bot_token="")
    assert backend.get_reactions(channel="C1", ts="123") == []


def test_slack_get_reactions_skips_non_dict_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "message": {
                    "reactions": [
                        "garbage",
                        {"name": "eyes"},
                        {"name": 42},  # name not a string
                    ],
                },
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(slack_http.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test")

    assert backend.get_reactions(channel="C1", ts="123") == ["eyes"]


def test_slack_get_reactions_returns_empty_when_reactions_not_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "message": {"reactions": "not-a-list"}},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(slack_http.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test")

    assert backend.get_reactions(channel="C1", ts="123") == []


def test_slack_get_reactions_returns_empty_when_get_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json={"ok": False}, request=httpx.Request("GET", url))

    monkeypatch.setattr(slack_http.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test")

    assert backend.get_reactions(channel="C1", ts="123") == []


def test_slack_fetch_dms_polls_conversations_history(monkeypatch: pytest.MonkeyPatch) -> None:
    call_log: list[str] = []

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        call_log.append(url)
        if "conversations.open" in url:
            return httpx.Response(200, json={"ok": True, "channel": {"id": "D99"}}, request=httpx.Request("POST", url))
        if "auth.test" in url:
            return httpx.Response(200, json={"ok": True, "user_id": "UBOT"}, request=httpx.Request("POST", url))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        call_log.append(url)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [
                    {"user": "UHUMAN", "text": "hello", "ts": "1.0"},
                    {"user": "UBOT", "text": "bot reply", "ts": "2.0"},
                ],
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(slack_http.httpx, "post", fake_post)
    monkeypatch.setattr(slack_http.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test", user_id="UHUMAN")

    dms = backend.fetch_dms(since="0.5")

    assert len(dms) == 1
    assert dms[0]["user"] == "UHUMAN"


def test_slack_fetch_dms_returns_empty_when_no_user_id() -> None:
    backend = SlackBotBackend(bot_token="xoxb-test", user_id="")
    assert backend.fetch_dms() == []


def test_slack_fetch_dms_returns_empty_when_dm_channel_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json={"ok": False}, request=httpx.Request("POST", url))

    monkeypatch.setattr(slack_http.httpx, "post", fake_post)
    backend = SlackBotBackend(bot_token="xoxb-test", user_id="U1")

    assert backend.fetch_dms() == []


def test_slack_fetch_dms_filters_non_dict_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        if "conversations.open" in url:
            return httpx.Response(200, json={"ok": True, "channel": {"id": "D1"}}, request=httpx.Request("POST", url))
        if "auth.test" in url:
            return httpx.Response(200, json={"ok": True, "user_id": "UBOT"}, request=httpx.Request("POST", url))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "messages": ["not-a-dict", {"user": "UHUMAN", "text": "ok", "ts": "1.0"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(slack_http.httpx, "post", fake_post)
    monkeypatch.setattr(slack_http.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test", user_id="UHUMAN")

    dms = backend.fetch_dms()
    assert len(dms) == 1


def test_slack_fetch_dms_returns_empty_when_messages_not_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        if "conversations.open" in url:
            return httpx.Response(200, json={"ok": True, "channel": {"id": "D1"}}, request=httpx.Request("POST", url))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "messages": "not-a-list"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(slack_http.httpx, "post", fake_post)
    monkeypatch.setattr(slack_http.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test", user_id="U1")

    assert backend.fetch_dms() == []


def test_slack_fetch_dms_returns_empty_when_api_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        if "conversations.open" in url:
            return httpx.Response(200, json={"ok": True, "channel": {"id": "D99"}}, request=httpx.Request("POST", url))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json={"ok": False}, request=httpx.Request("GET", url))

    monkeypatch.setattr(slack_http.httpx, "post", fake_post)
    monkeypatch.setattr(slack_http.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test", user_id="U1")

    assert backend.fetch_dms() == []


def test_slack_resolve_bot_id_caches_result(monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = 0

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        nonlocal call_count
        if "conversations.open" in url:
            return httpx.Response(200, json={"ok": True, "channel": {"id": "D1"}}, request=httpx.Request("POST", url))
        if "auth.test" in url:
            call_count += 1
            return httpx.Response(200, json={"ok": True, "user_id": "UBOT"}, request=httpx.Request("POST", url))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "messages": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(slack_http.httpx, "post", fake_post)
    monkeypatch.setattr(slack_http.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test", user_id="U1")

    backend.fetch_dms()
    backend.fetch_dms()
    assert call_count == 1


def test_slack_resolve_bot_id_returns_empty_on_api_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        if "conversations.open" in url:
            return httpx.Response(200, json={"ok": True, "channel": {"id": "D1"}}, request=httpx.Request("POST", url))
        if "auth.test" in url:
            return httpx.Response(200, json={"ok": False}, request=httpx.Request("POST", url))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "messages": [{"user": "UHUMAN", "text": "x", "ts": "1.0"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(slack_http.httpx, "post", fake_post)
    monkeypatch.setattr(slack_http.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test", user_id="U1")

    dms = backend.fetch_dms()
    assert len(dms) == 1


def test_slack_post_reply_includes_thread_ts(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        captured["json"] = kwargs["json"]
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(slack_http.httpx, "post", fake_post)
    backend = SlackBotBackend(bot_token="xoxb-test")

    backend.post_reply(channel="C", ts="1.0", text="reply")

    assert captured["json"] == {"channel": "C", "thread_ts": "1.0", "text": "reply"}


def test_slack_resolve_user_id_returns_empty_when_members_not_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "members": "not-a-list"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(slack_http.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test")

    assert backend.resolve_user_id("alice") == ""


def test_slack_fetch_dms_stamps_channel_on_each_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stamp ``channel`` so ``PendingChatInjection.record`` accepts the row (#1043).

    Without this the ``conversations.history`` payload returns each message
    with no ``channel`` field, the record guard ``if not channel`` discards
    every user DM, and the inbound bridge is a silent no-op.
    """

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        if "conversations.open" in url:
            return httpx.Response(200, json={"ok": True, "channel": {"id": "DXYZ"}}, request=httpx.Request("POST", url))
        if "auth.test" in url:
            return httpx.Response(200, json={"ok": True, "user_id": "UBOT"}, request=httpx.Request("POST", url))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        if "conversations.history" in url:
            return httpx.Response(
                200,
                json={"ok": True, "messages": [{"user": "UHUMAN", "text": "hi", "ts": "1.0"}]},
                request=httpx.Request("GET", url),
            )
        return httpx.Response(200, json={"ok": True, "messages": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(slack_http.httpx, "post", fake_post)
    monkeypatch.setattr(slack_http.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test", user_id="UHUMAN")

    dms = backend.fetch_dms()

    assert len(dms) == 1
    assert dms[0]["channel"] == "DXYZ"


def test_slack_fetch_dms_preserves_existing_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Socket Mode events already carry ``channel``; stamping must not overwrite it."""

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        if "conversations.open" in url:
            return httpx.Response(200, json={"ok": True, "channel": {"id": "DXYZ"}}, request=httpx.Request("POST", url))
        if "auth.test" in url:
            return httpx.Response(200, json={"ok": True, "user_id": "UBOT"}, request=httpx.Request("POST", url))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [{"user": "UHUMAN", "text": "hi", "ts": "1.0", "channel": "DSOCKET"}],
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(slack_http.httpx, "post", fake_post)
    monkeypatch.setattr(slack_http.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test", user_id="UHUMAN")

    dms = backend.fetch_dms()
    assert dms[0]["channel"] == "DSOCKET"


def test_slack_fetch_dms_includes_thread_replies(monkeypatch: pytest.MonkeyPatch) -> None:
    """For #1044: a user reply on a bot-DM thread must be returned alongside top-level DMs."""

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        if "conversations.open" in url:
            return httpx.Response(200, json={"ok": True, "channel": {"id": "DXYZ"}}, request=httpx.Request("POST", url))
        if "auth.test" in url:
            return httpx.Response(200, json={"ok": True, "user_id": "UBOT"}, request=httpx.Request("POST", url))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        if "conversations.history" in url:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [
                        # Bot-authored thread root with at least one reply (Slack returns
                        # ``thread_ts == ts`` on the root of a thread).
                        {"user": "UBOT", "text": "bot post", "ts": "5.0", "thread_ts": "5.0", "reply_count": 1},
                    ],
                },
                request=httpx.Request("GET", url),
            )
        if "conversations.replies" in url:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [
                        {"user": "UBOT", "text": "bot post", "ts": "5.0"},
                        {"user": "UHUMAN", "text": "thread reply", "ts": "5.1"},
                    ],
                },
                request=httpx.Request("GET", url),
            )
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", url))

    monkeypatch.setattr(slack_http.httpx, "post", fake_post)
    monkeypatch.setattr(slack_http.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test", user_id="UHUMAN")

    dms = backend.fetch_dms()

    # Bot's top-level post is filtered out; the user's thread reply is included.
    assert [e["ts"] for e in dms] == ["5.1"]
    assert dms[0]["channel"] == "DXYZ"


def test_slack_fetch_channel_history_returns_messages_with_channel_stamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``fetch_channel_history`` polls ``conversations.history`` and stamps channel (#1255).

    The SlackBroadcastsScanner wiring depends on the messaging backend
    exposing a per-channel history fetcher. The bot path returns the
    raw messages list with ``channel`` stamped so downstream consumers
    don't have to thread the channel argument back in.
    """
    captured: dict[str, object] = {}

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        return httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [
                    {"user": "UA", "text": "MR https://gitlab.example.com/team/p/-/merge_requests/1", "ts": "10.0"},
                    {"user": "UB", "text": "another", "ts": "20.0"},
                ],
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(slack_http.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test")

    messages = backend.fetch_channel_history(channel="C0AM3TENT", limit=10)

    assert "conversations.history" in str(captured["url"])
    assert [m["ts"] for m in messages] == ["10.0", "20.0"]
    assert all(m["channel"] == "C0AM3TENT" for m in messages)


def test_slack_fetch_channel_history_returns_empty_on_non_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "channel_not_found"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(slack_http.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test")

    assert backend.fetch_channel_history(channel="C404") == []


def test_slack_fetch_channel_history_returns_empty_without_channel() -> None:
    backend = SlackBotBackend(bot_token="xoxb-test")
    assert backend.fetch_channel_history(channel="") == []


def test_slack_resolve_user_id_skips_non_dict_members(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "members": [
                    "garbage",
                    {"id": 42, "name": "alice"},  # id not a string
                    {"id": "U99", "name": "alice"},
                ],
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(slack_http.httpx, "get", fake_get)
    backend = SlackBotBackend(bot_token="xoxb-test")

    assert backend.resolve_user_id("alice") == "U99"
