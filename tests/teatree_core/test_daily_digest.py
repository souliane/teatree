"""Behaviour tests for the rolling daily DM digest thread (#672)."""

import datetime as dt
from unittest.mock import MagicMock

from django.test import TestCase

from teatree.core.daily_digest import DailyDigest
from teatree.core.models import DailyDigestMessage, DailyDigestThread


def _backend() -> MagicMock:
    b = MagicMock()
    b.open_dm.return_value = "D-USER"
    b.post_message.return_value = {"ok": True, "ts": "root-111"}
    b.post_reply.return_value = {"ok": True, "ts": "reply-222"}
    return b


def _at(y: int, mo: int, d: int, h: int = 12) -> dt.datetime:
    return dt.datetime(y, mo, d, h, tzinfo=dt.UTC)


class TestDailyDigest(TestCase):
    def test_first_post_of_day_opens_dm_and_creates_thread(self) -> None:
        backend = _backend()
        clock = _at(2026, 5, 15)
        digest = DailyDigest(backend=backend, user_id="U_ME", now=lambda: clock)

        thread = digest.post("first message", idempotency_key="k1")

        backend.open_dm.assert_called_once_with("U_ME")
        backend.post_message.assert_called_once()  # root opener
        backend.post_reply.assert_called_once_with(channel="D-USER", ts="root-111", text="first message")
        assert thread.date == dt.date(2026, 5, 15)
        assert thread.channel_ref == "D-USER"
        assert thread.root_ts == "root-111"
        assert DailyDigestMessage.objects.filter(thread=thread).count() == 1

    def test_same_day_reuses_thread_no_new_root(self) -> None:
        backend = _backend()
        clock = _at(2026, 5, 15, 9)
        digest = DailyDigest(backend=backend, user_id="U_ME", now=lambda: clock)

        digest.post("morning", idempotency_key="k1")
        clock = _at(2026, 5, 15, 18)
        digest.post("evening", idempotency_key="k2")

        backend.open_dm.assert_called_once()  # only once for the day
        backend.post_message.assert_called_once()  # root opened once
        assert backend.post_reply.call_count == 2
        assert DailyDigestThread.objects.count() == 1

    def test_next_utc_day_rolls_to_new_thread(self) -> None:
        backend = _backend()
        clock = _at(2026, 5, 15, 23)
        digest = DailyDigest(backend=backend, user_id="U_ME", now=lambda: clock)
        digest.post("late on the 15th", idempotency_key="k1")

        clock = _at(2026, 5, 16, 1)
        digest.post("early on the 16th", idempotency_key="k2")

        assert DailyDigestThread.objects.count() == 2
        assert set(DailyDigestThread.objects.values_list("date", flat=True)) == {
            dt.date(2026, 5, 15),
            dt.date(2026, 5, 16),
        }
        assert backend.open_dm.call_count == 2

    def test_duplicate_idempotency_key_does_not_repost(self) -> None:
        backend = _backend()
        clock = _at(2026, 5, 15)
        digest = DailyDigest(backend=backend, user_id="U_ME", now=lambda: clock)

        digest.post("once", idempotency_key="dup")
        digest.post("once", idempotency_key="dup")

        assert backend.post_reply.call_count == 1
        assert DailyDigestMessage.objects.filter(idempotency_key="dup").count() == 1

    def test_close_with_recap_sets_closed_and_posts_once(self) -> None:
        backend = _backend()
        clock = _at(2026, 5, 15)
        digest = DailyDigest(backend=backend, user_id="U_ME", now=lambda: clock)
        digest.post("work happened", idempotency_key="k1")

        digest.close_with_recap("3 PRs merged, 0 failures")
        digest.close_with_recap("second call is a no-op")

        thread = DailyDigestThread.objects.get()
        assert thread.closed_at is not None
        # 1 work message + exactly 1 recap reply
        assert backend.post_reply.call_count == 2

    def test_post_after_close_still_threads_but_does_not_reopen(self) -> None:
        backend = _backend()
        clock = _at(2026, 5, 15)
        digest = DailyDigest(backend=backend, user_id="U_ME", now=lambda: clock)
        digest.post("a", idempotency_key="k1")
        digest.close_with_recap("recap")

        digest.post("late straggler", idempotency_key="k2")

        thread = DailyDigestThread.objects.get()
        assert thread.closed_at is not None  # stays closed
        assert backend.post_message.call_count == 1  # never reopened a root
