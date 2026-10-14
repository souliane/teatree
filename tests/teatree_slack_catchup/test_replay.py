"""The public replay iterator reads bounded slices in forward order."""

import time

import pytest

from teatree.backends.slack import http as slack_http
from teatree.backends.slack.bot import SlackBotBackend
from teatree.backends.slack.bot_errors import SlackReadRefusedError
from teatree.slack_catchup import catch_up
from teatree.types import RawAPIDict
from tests.integration.slack_bridge_e2e.conftest import FakeSlackTransport, serve_history


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
