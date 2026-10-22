import datetime as dt

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import ReviewRequestPost
from teatree.loop.scanners.review_nag_claim import ReviewNagClaim, ReviewNagPostReconciliationError


def _request(number: int, *, thread: str = "1000", done: bool = False) -> ReviewRequestPost:
    return ReviewRequestPost.objects.create(
        mr_url=f"https://gitlab.example/team/-/merge_requests/{number}",
        overlay="",
        slack_channel_id="CREVIEW",
        slack_thread_ts=thread,
        done_at=timezone.now() if done else None,
    )


class TestNagClaim(TestCase):
    def test_placeholder_posts_with_no_thread_coordinates_are_separate(self) -> None:
        first = _request(1)
        second = _request(2)
        ReviewRequestPost.objects.filter(pk__in=[first.pk, second.pk]).update(slack_channel_id="", slack_thread_ts="")
        first.refresh_from_db()

        assert list(ReviewRequestPost.objects.open_thread(first).values_list("pk", flat=True)) == [first.pk]

    def test_failed_post_releases_each_previous_step(self) -> None:
        first = _request(1)
        second = _request(2)
        prior = timezone.now() - dt.timedelta(days=10)
        ReviewRequestPost.objects.filter(pk=first.pk).update(last_nag_at=prior, nag_count=2)
        ReviewRequestPost.objects.filter(pk=second.pk).update(last_nag_at=prior, nag_count=1)
        now = timezone.now()
        first.refresh_from_db()
        claim = ReviewNagClaim.take(first, now, dt.timedelta(days=4))

        assert claim is not None
        assert claim.nag_count == 3
        claim.release()
        first.refresh_from_db()
        second.refresh_from_db()
        assert (first.last_nag_at, first.nag_count) == (prior, 2)
        assert (second.last_nag_at, second.nag_count) == (prior, 1)

    def test_lower_count_claimant_advances_past_sibling_count(self) -> None:
        first = _request(1)
        second = _request(2)
        ReviewRequestPost.objects.filter(pk=first.pk).update(nag_count=1)
        ReviewRequestPost.objects.filter(pk=second.pk).update(nag_count=3)

        claim = ReviewNagClaim.take(first, timezone.now(), dt.timedelta(days=2))

        assert claim is not None
        assert claim.nag_count == 4
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.nag_count == second.nag_count == 4

    def test_closed_claimant_cannot_advance_an_open_sibling(self) -> None:
        closed = _request(1, done=True)
        open_post = _request(2)

        assert ReviewNagClaim.take(closed, timezone.now(), dt.timedelta(days=2)) is None
        open_post.refresh_from_db()
        assert open_post.last_nag_at is None
        assert open_post.nag_count == 0

    def test_release_does_not_overwrite_a_newer_count(self) -> None:
        post = _request(1)
        claim = ReviewNagClaim.take(post, timezone.now(), dt.timedelta(days=2))
        assert claim is not None
        ReviewRequestPost.objects.filter(pk=post.pk).update(nag_count=2)

        claim.release()

        post.refresh_from_db()
        assert post.last_nag_at == claim.claimed_at
        assert post.nag_count == 2

    def test_release_does_not_overwrite_a_newer_timestamp(self) -> None:
        post = _request(1)
        claim = ReviewNagClaim.take(post, timezone.now(), dt.timedelta(days=2))
        assert claim is not None
        later = claim.claimed_at + dt.timedelta(seconds=1)
        ReviewRequestPost.objects.filter(pk=post.pk).update(last_nag_at=later)

        claim.release()

        post.refresh_from_db()
        assert post.last_nag_at == later
        assert post.nag_count == 1

    def test_reconciliation_mismatch_rolls_back_all_reply_timestamps(self) -> None:
        first = _request(1)
        second = _request(2)
        claim = ReviewNagClaim.take(first, timezone.now(), dt.timedelta(days=2))

        assert claim is not None
        ReviewRequestPost.objects.filter(pk=second.pk).update(nag_count=99)
        with pytest.raises(ReviewNagPostReconciliationError):
            claim.persist_reply_ts("reply.1")

        first.refresh_from_db()
        second.refresh_from_db()
        assert first.last_nag_reply_ts == ""
        assert second.last_nag_reply_ts == ""
