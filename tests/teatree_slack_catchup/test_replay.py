"""The public replay iterator reads bounded slices in forward order."""

import time

import pytest

from teatree.backends.slack import http as slack_http
from teatree.backends.slack.bot import SlackBotBackend
from teatree.backends.slack.bot_errors import SlackReadRefusedError
from teatree.slack_catchup import catch_up
from teatree.slack_catchup.window import CatchUpRefusedError
from teatree.types import RawAPIDict
from tests.integration.slack_bridge_e2e.conftest import FakeSlackTransport, serve_history


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> FakeSlackTransport:
    fake = FakeSlackTransport()
    monkeypatch.setattr(slack_http.httpx, "post", fake.post)
    monkeypatch.setattr(slack_http.httpx, "get", fake.get)
    return fake


def test_each_next_reads_one_full_slice(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeSlackTransport()
    monkeypatch.setattr(slack_http.httpx, "post", transport.post)
    monkeypatch.setattr(slack_http.httpx, "get", transport.get)
    now = time.time()
    old, recent = f"{now - 2 * 86400 + 30:.6f}", f"{now - 30:.6f}"
    serve_history(
        transport,
        [{"ts": old, "user": "U_HUMAN", "text": "old"}, {"ts": recent, "user": "U_HUMAN", "text": "recent"}],
    )
    backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")
    delivered: list[RawAPIDict] = []

    def deliver(event: RawAPIDict) -> bool:
        delivered.append(event)
        return True

    replay = catch_up(backend, alive_at=now - 2 * 86400, deliver=deliver)
    assert transport.calls_to("conversations.history") == []
    first = next(replay)
    assert len(transport.calls_to("conversations.history")) == 1
    remaining = list(replay)

    assert [event["ts"] for event in delivered] == [old, recent]
    assert first.caught_up_to < remaining[-1].caught_up_to
    assert all(item.delivered in {0, 1} for item in [first, *remaining])


def test_refused_history_page_cap_delivers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeSlackTransport()
    monkeypatch.setattr(slack_http.httpx, "post", transport.post)
    monkeypatch.setattr(slack_http.httpx, "get", transport.get)
    transport.default_responses["conversations.history"] = {
        "ok": True,
        "messages": [],
        "response_metadata": {"next_cursor": "more"},
    }
    backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")
    delivered: list[RawAPIDict] = []

    def deliver(event: RawAPIDict) -> bool:
        delivered.append(event)
        return True

    with pytest.raises(SlackReadRefusedError, match="page_cap_reached"):
        list(catch_up(backend, alive_at=time.time() - 180, deliver=deliver))

    assert delivered == []


def test_messages_within_one_slice_deliver_oldest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeSlackTransport()
    monkeypatch.setattr(slack_http.httpx, "post", transport.post)
    monkeypatch.setattr(slack_http.httpx, "get", transport.get)
    now = time.time()
    older, newer = f"{now - 90:.6f}", f"{now - 30:.6f}"
    serve_history(
        transport,
        [{"ts": newer, "user": "U_HUMAN", "text": "new"}, {"ts": older, "user": "U_HUMAN", "text": "old"}],
    )
    backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")
    delivered: list[RawAPIDict] = []

    def deliver(event: RawAPIDict) -> bool:
        delivered.append(event)
        return True

    list(catch_up(backend, alive_at=now - 180, deliver=deliver))

    assert [event["ts"] for event in delivered] == [older, newer]


def test_refused_replies_page_cap_delivers_nothing(transport: FakeSlackTransport) -> None:
    now = time.time()
    root = f"{now - 90:.6f}"
    serve_history(
        transport,
        [{"ts": root, "user": "U_HUMAN", "text": "root"}],
        {root: [{"ts": f"{now - 30:.6f}", "user": "U_HUMAN", "text": "reply"}]},
    )
    transport.handlers["conversations.replies"] = lambda _params, _headers: {
        "ok": True,
        "messages": [],
        "response_metadata": {"next_cursor": "more"},
    }
    delivered: list[RawAPIDict] = []

    with pytest.raises(SlackReadRefusedError, match="page_cap_reached"):
        list(
            catch_up(
                SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN"),
                alive_at=now - 180,
                deliver=lambda event: delivered.append(event) or True,
            )
        )

    assert len(transport.calls_to("conversations.replies")) == 40
    assert delivered == []


def test_refused_membership_page_cap_delivers_nothing(transport: FakeSlackTransport) -> None:
    now = time.time()
    serve_history(transport, [{"ts": f"{now - 30:.6f}", "user": "U_HUMAN", "text": "dm"}])
    transport.default_responses["users.conversations"] = {
        "ok": True,
        "channels": [{"id": "C_MEMBER"}],
        "response_metadata": {"next_cursor": "more"},
    }
    delivered: list[RawAPIDict] = []

    with pytest.raises(SlackReadRefusedError, match="page_cap_reached"):
        list(
            catch_up(
                SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user", user_id="U_HUMAN"),
                alive_at=now - 180,
                deliver=lambda event: delivered.append(event) or True,
            )
        )

    assert len(transport.calls_to("users.conversations")) == 20
    assert delivered == []


def test_refused_reactions_page_cap_delivers_nothing(transport: FakeSlackTransport) -> None:
    now = time.time()
    serve_history(transport, [{"ts": f"{now - 30:.6f}", "user": "U_HUMAN", "text": "dm"}])
    transport.default_responses["users.conversations"] = {"ok": True, "channels": [{"id": "C_MEMBER"}]}
    transport.default_responses["search.messages"] = {"ok": True, "messages": {"matches": [], "paging": {"pages": 1}}}
    transport.default_responses["reactions.list"] = {
        "ok": True,
        "items": [],
        "response_metadata": {"next_cursor": "more"},
    }
    delivered: list[RawAPIDict] = []

    with pytest.raises(SlackReadRefusedError, match="page_cap_reached"):
        list(
            catch_up(
                SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user", user_id="U_HUMAN"),
                alive_at=now - 180,
                deliver=lambda event: delivered.append(event) or True,
            )
        )

    assert len(transport.calls_to("reactions.list")) == 20
    assert delivered == []


def test_refused_search_page_cap_delivers_nothing(transport: FakeSlackTransport) -> None:
    now = time.time()
    serve_history(transport, [{"ts": f"{now - 30:.6f}", "user": "U_HUMAN", "text": "dm"}])
    transport.default_responses["users.conversations"] = {"ok": True, "channels": [{"id": "C_MEMBER"}]}
    transport.default_responses["search.messages"] = {"ok": True, "messages": {"matches": [], "paging": {"pages": 101}}}
    delivered: list[RawAPIDict] = []

    with pytest.raises(CatchUpRefusedError, match=r"search\.messages page cap reached"):
        list(
            catch_up(
                SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user", user_id="U_HUMAN"),
                alive_at=now - 180,
                deliver=lambda event: delivered.append(event) or True,
            )
        )

    assert len(transport.calls_to("search.messages")) == 100
    assert delivered == []


def test_search_second_page_delivers_member_channel_match(transport: FakeSlackTransport) -> None:
    now = time.time()
    stamp = f"{now - 30:.6f}"
    transport.default_responses["users.conversations"] = {"ok": True, "channels": [{"id": "C_MEMBER"}]}
    transport.handlers["search.messages"] = lambda params, _headers: {
        "ok": True,
        "messages": {
            "matches": [{"ts": stamp, "user": "U_OTHER", "channel": {"id": "C_MEMBER"}}] if params["page"] == 2 else [],
            "paging": {"pages": 2},
        },
    }
    transport.default_responses["reactions.list"] = {"ok": True, "items": []}
    delivered: list[RawAPIDict] = []

    list(
        catch_up(
            SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user", user_id="U_HUMAN"),
            alive_at=now - 180,
            deliver=lambda event: delivered.append(event) or True,
        )
    )

    assert [call.payload["page"] for call in transport.calls_to("search.messages")] == [1, 2]
    assert [(event["type"], event["channel"]) for event in delivered] == [("app_mention", "C_MEMBER")]


def test_reaction_before_oldest_is_not_delivered(transport: FakeSlackTransport) -> None:
    now = time.time()
    transport.default_responses["users.conversations"] = {"ok": True, "channels": [{"id": "C_MEMBER"}]}
    transport.default_responses["search.messages"] = {"ok": True, "messages": {"matches": [], "paging": {"pages": 1}}}
    transport.default_responses["reactions.list"] = {
        "ok": True,
        "items": [
            {
                "type": "message",
                "channel": "C_MEMBER",
                "message": {"ts": f"{now - 86400 - 300:.6f}", "reactions": [{"name": "eyes", "users": ["U_HUMAN"]}]},
            }
        ],
    }
    delivered: list[RawAPIDict] = []

    list(
        catch_up(
            SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user", user_id="U_HUMAN"),
            alive_at=now - 180,
            deliver=lambda event: delivered.append(event) or True,
        )
    )

    assert transport.calls_to("reactions.list")
    assert delivered == []
