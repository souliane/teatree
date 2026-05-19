"""Tests for the ReviewRequestPost model (#1038).

Tracks bot posts to the review channel so the fibonacci nag scanner
can detect already-posted MRs and escalate at +1/+2/+3/+5 days.
"""

import datetime as dt

import pytest
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import ReviewRequestPost


class TestReviewRequestPostModel(TestCase):
    def test_create_minimal_row(self) -> None:
        post = ReviewRequestPost.objects.create(
            mr_url="https://gitlab.example/x/-/merge_requests/1",
            slack_channel_id="C0AM3TENTLK",
            slack_thread_ts="1700000000.000100",
        )
        assert post.pk is not None
        assert post.last_nag_step == 0
        assert post.done_at is None
        assert post.bot_id == ""
        assert post.created_at is not None

    def test_mr_url_is_unique(self) -> None:
        ReviewRequestPost.objects.create(
            mr_url="https://gitlab.example/x/-/merge_requests/2",
            slack_channel_id="C0AM3TENTLK",
            slack_thread_ts="1700000001.000200",
        )
        with pytest.raises(IntegrityError):
            ReviewRequestPost.objects.create(
                mr_url="https://gitlab.example/x/-/merge_requests/2",
                slack_channel_id="C0AM3TENTLK",
                slack_thread_ts="1700000002.000300",
            )

    def test_str_representation_carries_url_and_step(self) -> None:
        post = ReviewRequestPost.objects.create(
            mr_url="https://gitlab.example/x/-/merge_requests/3",
            slack_channel_id="C0AM3TENTLK",
            slack_thread_ts="1700000003.000400",
            last_nag_step=2,
        )
        rendered = str(post)
        assert "merge_requests/3" in rendered
        assert "step=2" in rendered

    def test_done_at_can_be_set(self) -> None:
        when = timezone.now()
        post = ReviewRequestPost.objects.create(
            mr_url="https://gitlab.example/x/-/merge_requests/4",
            slack_channel_id="C0AM3TENTLK",
            slack_thread_ts="1700000004.000500",
            done_at=when,
        )
        assert post.done_at == when

    def test_default_ordering_is_recent_first(self) -> None:
        old = ReviewRequestPost.objects.create(
            mr_url="https://gitlab.example/x/-/merge_requests/10",
            slack_channel_id="C",
            slack_thread_ts="1",
            created_at=timezone.now() - dt.timedelta(days=2),
        )
        new = ReviewRequestPost.objects.create(
            mr_url="https://gitlab.example/x/-/merge_requests/11",
            slack_channel_id="C",
            slack_thread_ts="2",
            created_at=timezone.now(),
        )
        ordered = list(ReviewRequestPost.objects.all())
        assert ordered[0].pk == new.pk
        assert ordered[1].pk == old.pk
