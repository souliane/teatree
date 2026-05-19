"""Tests for record_review_request_post (#1038)."""

from django.test import TestCase

from teatree.core.models import ReviewRequestPost
from teatree.loop.review_request_tracker import record_review_request_post


class TestRecordReviewRequestPost(TestCase):
    def test_creates_new_row(self) -> None:
        post = record_review_request_post(
            mr_url="https://gitlab.example/x/-/merge_requests/1",
            slack_channel_id="C0DEMOCHAN1",
            slack_thread_ts="1700000000.001",
            bot_id="B123",
        )
        assert post.pk is not None
        assert post.bot_id == "B123"
        assert post.last_nag_step == 0
        assert post.done_at is None

    def test_idempotent_re_post_updates_thread_reference(self) -> None:
        record_review_request_post(
            mr_url="https://gitlab.example/x/-/merge_requests/2",
            slack_channel_id="C0DEMOCHAN1",
            slack_thread_ts="1700000000.001",
        )
        # Bump the nag step to simulate a real escalation already in flight.
        post = ReviewRequestPost.objects.get(mr_url="https://gitlab.example/x/-/merge_requests/2")
        post.last_nag_step = 2
        post.save()

        updated = record_review_request_post(
            mr_url="https://gitlab.example/x/-/merge_requests/2",
            slack_channel_id="C0DEMOCHAN1",
            slack_thread_ts="1700000999.999",
        )
        assert updated.pk == post.pk
        # State machine state is preserved across re-posts.
        assert updated.last_nag_step == 2
        # Thread reference is refreshed.
        assert updated.slack_thread_ts == "1700000999.999"
        # Only one row total.
        assert ReviewRequestPost.objects.count() == 1

    def test_re_post_updates_bot_id_when_provided(self) -> None:
        record_review_request_post(
            mr_url="https://gitlab.example/x/-/merge_requests/3",
            slack_channel_id="C0DEMOCHAN1",
            slack_thread_ts="1.0",
            bot_id="B_OLD",
        )
        updated = record_review_request_post(
            mr_url="https://gitlab.example/x/-/merge_requests/3",
            slack_channel_id="C0DEMOCHAN1",
            slack_thread_ts="2.0",
            bot_id="B_NEW",
        )
        assert updated.bot_id == "B_NEW"

    def test_re_post_does_not_clobber_bot_id_when_empty(self) -> None:
        record_review_request_post(
            mr_url="https://gitlab.example/x/-/merge_requests/4",
            slack_channel_id="C0DEMOCHAN1",
            slack_thread_ts="1.0",
            bot_id="B_ORIGINAL",
        )
        updated = record_review_request_post(
            mr_url="https://gitlab.example/x/-/merge_requests/4",
            slack_channel_id="C0DEMOCHAN1",
            slack_thread_ts="2.0",
        )
        # Empty bot_id should not erase the original.
        assert updated.bot_id == "B_ORIGINAL"
