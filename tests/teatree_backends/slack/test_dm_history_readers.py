"""Branch coverage for the dm_history single-message and thread-reply readers.

The two raw ``conversations.*`` read helpers split out of ``SlackBotBackend``
to keep the backend under the module-health LOC cap (#2061). Pure functions
over an injected ``get`` callable — no Slack network, no Django.
"""

import logging

import pytest

from teatree.backends.slack.dm_history import _MAX_THREAD_PAGES, read_single_message, read_thread_replies, read_user_dms
from teatree.types import RawAPIDict


def _ok(messages: list[RawAPIDict]) -> RawAPIDict:
    return {"ok": True, "messages": messages}


def _page(messages: list[RawAPIDict], next_cursor: str = "") -> RawAPIDict:
    data = _ok(messages)
    if next_cursor:
        data["response_metadata"] = {"next_cursor": next_cursor}
    return data


class TestReadSingleMessage:
    def test_returns_first_message_with_channel_stamped(self) -> None:
        def get(method: str, params: dict[str, str | int]) -> RawAPIDict:
            assert method == "conversations.history"
            assert params["latest"] == "1.0"
            return _ok([{"ts": "1.0", "text": "hi"}])

        message = read_single_message(get=get, channel="D1", ts="1.0")

        assert message == {"ts": "1.0", "text": "hi", "channel": "D1"}

    def test_empty_channel_or_ts_short_circuits(self) -> None:
        calls: list[str] = []

        def get(method: str, params: dict[str, str | int]) -> RawAPIDict:
            calls.append(method)
            return {}

        assert read_single_message(get=get, channel="", ts="1.0") == {}
        assert read_single_message(get=get, channel="D1", ts="") == {}
        assert calls == []  # never hit Slack on an empty key

    def test_non_ok_response_returns_empty(self) -> None:
        message = read_single_message(get=lambda *_: {"ok": False}, channel="D1", ts="1.0")
        assert message == {}

    def test_no_matching_message_returns_empty(self) -> None:
        message = read_single_message(get=lambda *_: _ok([]), channel="D1", ts="1.0")
        assert message == {}

    def test_does_not_clobber_an_existing_channel_field(self) -> None:
        message = read_single_message(get=lambda *_: _ok([{"ts": "1.0", "channel": "ALREADY"}]), channel="D1", ts="1.0")
        assert message["channel"] == "ALREADY"


class TestReadThreadReplies:
    def test_returns_every_reply_with_channel_stamped(self) -> None:
        def get(method: str, params: dict[str, str | int]) -> RawAPIDict:
            assert method == "conversations.replies"
            assert params["ts"] == "root.ts"
            return _ok([{"ts": "root.ts", "text": "q"}, {"ts": "r1", "text": "a"}])

        replies = read_thread_replies(get=get, channel="D1", thread_ts="root.ts")

        assert [r["ts"] for r in replies] == ["root.ts", "r1"]
        assert all(r["channel"] == "D1" for r in replies)

    def test_empty_channel_or_root_short_circuits(self) -> None:
        calls: list[str] = []

        def get(method: str, params: dict[str, str | int]) -> RawAPIDict:
            calls.append(method)
            return {}

        assert read_thread_replies(get=get, channel="", thread_ts="root.ts") == []
        assert read_thread_replies(get=get, channel="D1", thread_ts="") == []
        assert calls == []  # never hit Slack on an empty key

    def test_non_ok_response_returns_empty(self) -> None:
        assert read_thread_replies(get=lambda *_: {"ok": False}, channel="D1", thread_ts="root.ts") == []

    def test_follows_the_cursor_across_pages(self) -> None:
        pages = {
            "": _page([{"ts": "root.ts"}, {"ts": "r1"}], next_cursor="c1"),
            "c1": _page([{"ts": "r2"}, {"ts": "r3"}], next_cursor="c2"),
            "c2": _page([{"ts": "r4"}]),
        }

        def get(method: str, params: dict[str, str | int]) -> RawAPIDict:
            assert method == "conversations.replies"
            return pages[str(params.get("cursor", ""))]

        replies = read_thread_replies(get=get, channel="D1", thread_ts="root.ts")

        assert [r["ts"] for r in replies] == ["root.ts", "r1", "r2", "r3", "r4"]
        assert all(r["channel"] == "D1" for r in replies)

    def test_page_cap_logs_and_stops(self, caplog: pytest.LogCaptureFixture) -> None:
        def get(method: str, params: dict[str, str | int]) -> RawAPIDict:
            return _page([{"ts": str(params.get("cursor", "start"))}], next_cursor="more")

        with caplog.at_level(logging.WARNING):
            replies = read_thread_replies(get=get, channel="D1", thread_ts="root.ts")

        assert len(replies) == _MAX_THREAD_PAGES
        assert "hit the" in caplog.text


class TestReadUserDmsThreadFanout:
    def test_thread_fanout_follows_the_cursor(self) -> None:
        history = _ok([{"ts": "root.ts", "thread_ts": "root.ts", "reply_count": 1}])
        reply_pages = {
            "": _page([{"ts": "root.ts"}, {"ts": "r1"}], next_cursor="c1"),
            "c1": _page([{"ts": "r2"}]),
        }

        def get(method: str, params: dict[str, str | int]) -> RawAPIDict:
            if method == "conversations.history":
                return history
            return reply_pages[str(params.get("cursor", ""))]

        messages = read_user_dms(get=get, channel="D1", since="", identity=None)

        assert [m["ts"] for m in messages] == ["root.ts", "r1", "r2"]
