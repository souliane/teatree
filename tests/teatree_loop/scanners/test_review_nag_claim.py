import datetime as dt

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import ReviewRequestPost
from teatree.loop.scanners.review_nag_claim import ReviewNagPostReconciliationError, _NagClaim


def _request(number: int, *, thread: str = "1000", done: bool = False) -> ReviewRequestPost:
    return ReviewRequestPost.objects.create(
        mr_url=f"https://gitlab.example/team/-/merge_requests/{number}",
        overlay="",
        slack_channel_id="CREVIEW",
        slack_thread_ts=thread,
        done_at=timezone.now() if done else None,
    )


class TestNagClaim(TestCase):
    def test_confirmed_reply_advances_only_open_siblings_in_one_thread(self) -> None:
        first = _request(1)
        second = _request(2)
        other = _request(3, thread="2000")
        closed = _request(4, done=True)
        now = timezone.now()

        claim = _NagClaim.take(first, now, dt.timedelta(days=2))

        assert claim is not None
        claim.persist_reply_ts("reply.1")
        for post in (first, second):
            post.refresh_from_db()
            assert post.last_nag_at == now
            assert post.nag_count == 1
            assert post.last_nag_reply_ts == "reply.1"
        for post in (other, closed):
            post.refresh_from_db()
            assert post.last_nag_at is None
            assert post.nag_count == 0

    def test_recent_peer_refuses_a_second_claim(self) -> None:
        first = _request(1)
        second = _request(2)
        now = timezone.now()
        initial = _NagClaim.take(first, now, dt.timedelta(days=2))

        assert initial is not None
        assert _NagClaim.take(second, now, dt.timedelta(days=2)) is None

    def test_failed_post_releases_each_previous_step(self) -> None:
        first = _request(1)
        second = _request(2)
        prior = timezone.now() - dt.timedelta(days=10)
        ReviewRequestPost.objects.filter(pk=first.pk).update(last_nag_at=prior, nag_count=2)
        ReviewRequestPost.objects.filter(pk=second.pk).update(last_nag_at=prior, nag_count=1)
        now = timezone.now()
        first.refresh_from_db()
        claim = _NagClaim.take(first, now, dt.timedelta(days=4))

        assert claim is not None
        assert claim.nag_count == 3
        claim.release()
        first.refresh_from_db()
        second.refresh_from_db()
        assert (first.last_nag_at, first.nag_count) == (prior, 2)
        assert (second.last_nag_at, second.nag_count) == (prior, 1)

    def test_reconciliation_mismatch_rolls_back_all_reply_timestamps(self) -> None:
        first = _request(1)
        second = _request(2)
        claim = _NagClaim.take(first, timezone.now(), dt.timedelta(days=2))

        assert claim is not None
        ReviewRequestPost.objects.filter(pk=second.pk).update(nag_count=99)
        with pytest.raises(ReviewNagPostReconciliationError):
            claim.persist_reply_ts("reply.1")

        first.refresh_from_db()
        second.refresh_from_db()
        assert first.last_nag_reply_ts == ""
        assert second.last_nag_reply_ts == ""
