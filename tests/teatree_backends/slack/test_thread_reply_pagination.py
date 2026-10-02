"""``read_thread_activity`` walks ``conversations.replies`` by cursor.

Slack pages thread replies OLDEST-first, so a single ``limit:200`` read of a
250-reply thread yields the 200th reply as "the newest". Any freshness check
built on ``latest_reply_ts`` would then read a busy, actively-discussed thread as
stale — and it would do so silently, because a truncated read and a complete one
are the same shape.
"""

import pytest

from teatree.backends.slack import client as slack_client
from teatree.backends.slack.client import SlackThreadActivityRequest, ThreadActivityRead, read_thread_activity
from teatree.types import RawAPIDict

_CHANNEL = "C0REVIEW"
_PARENT_TS = "1700000000.000100"


class FakePagedSlack:
    """Serves the queued pages in order and records the cursor each read carried."""

    def __init__(self, pages: list[RawAPIDict]) -> None:
        self._pages = list(pages)
        self.cursors: list[str] = []

    def get(self, method: str, *, token: str = "", params: dict | None = None) -> RawAPIDict:
        self.cursors.append(str((params or {}).get("cursor", "")))
        return dict(self._pages.pop(0))


def _page(*, reply_ts: list[str], has_more: bool, cursor: str = "", repeat_parent: bool = True) -> RawAPIDict:
    messages: list[RawAPIDict] = [{"ts": _PARENT_TS}] if repeat_parent else []
    messages.extend({"ts": ts} for ts in reply_ts)
    page: RawAPIDict = {"ok": True, "messages": messages, "has_more": has_more}
    if cursor:
        page["response_metadata"] = {"next_cursor": cursor}
    return page


def _read(fake: FakePagedSlack, *, max_pages: int = 10) -> ThreadActivityRead:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(slack_client, "SlackHttpClient", lambda **_kw: fake)
        request = SlackThreadActivityRequest(
            token="xoxp-user", channel_id=_CHANNEL, thread_ts=_PARENT_TS, max_pages=max_pages
        )
        return read_thread_activity(request)


class TestLatestReplyAcrossPages:
    def test_the_newest_reply_comes_from_the_last_page(self) -> None:
        fake = FakePagedSlack(
            [
                _page(reply_ts=["1700000100.000000", "1700000200.000000"], has_more=True, cursor="c1"),
                _page(reply_ts=["1700000900.000000"], has_more=False),
            ]
        )
        read = _read(fake)
        assert read.latest_reply_ts == "1700000900.000000"
        assert read.replies_complete is True
        assert fake.cursors == ["", "c1"]

    def test_the_repeated_parent_is_not_mistaken_for_a_reply(self) -> None:
        # Slack repeats the thread root at the head of each page; counting it would
        # make an ancient parent ts win the newest-reply comparison on a thread
        # whose replies are all older than it cannot be — but it would also report
        # a reply where there is none.
        fake = FakePagedSlack(
            [
                _page(reply_ts=["1700000100.000000"], has_more=True, cursor="c1"),
                _page(reply_ts=[], has_more=False),
            ]
        )
        read = _read(fake)
        assert read.latest_reply_ts == "1700000100.000000"
        assert read.parent_ts == _PARENT_TS

    def test_a_single_page_thread_reads_one_page_only(self) -> None:
        fake = FakePagedSlack([_page(reply_ts=["1700000100.000000"], has_more=False)])
        read = _read(fake)
        assert read.latest_reply_ts == "1700000100.000000"
        assert read.replies_complete is True
        assert fake.cursors == [""]


class TestTruncationIsReported:
    def test_exhausting_the_page_budget_reports_an_incomplete_read(self) -> None:
        # The newest reply was never seen, so latest_reply_ts is not authoritative
        # and the flag says so rather than the value lying.
        pages = [
            _page(reply_ts=[f"17000001{index:02d}.000000"], has_more=True, cursor=f"c{index}") for index in range(6)
        ]
        fake = FakePagedSlack(pages)
        read = _read(fake, max_pages=3)
        assert read.ok is True
        assert read.exists is True
        assert read.replies_complete is False
        assert len(fake.cursors) == 3

    def test_a_not_ok_later_page_reports_an_incomplete_read(self) -> None:
        fake = FakePagedSlack(
            [
                _page(reply_ts=["1700000100.000000"], has_more=True, cursor="c1"),
                {"ok": False, "error": "ratelimited"},
            ]
        )
        read = _read(fake)
        assert read.replies_complete is False

    def test_has_more_with_no_cursor_ends_a_complete_read(self) -> None:
        # Nothing can advance the walk; the read finished with what Slack gave.
        fake = FakePagedSlack([_page(reply_ts=["1700000100.000000"], has_more=True)])
        read = _read(fake)
        assert read.replies_complete is True
        assert fake.cursors == [""]
