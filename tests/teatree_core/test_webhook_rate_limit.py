"""Tests for the per-source webhook token-bucket rate limiter (#673 item 3)."""

import json

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from teatree.core.models import IncomingEvent
from teatree.core.views._rate_limit import TokenBucket, WebhookRateLimiter


class TestTokenBucket:
    def test_allows_up_to_capacity_then_denies(self) -> None:
        clock = [1000.0]
        bucket = TokenBucket(capacity=3, refill_per_second=1.0, now=lambda: clock[0])

        assert [bucket.allow() for _ in range(4)] == [True, True, True, False]

    def test_refills_over_time(self) -> None:
        clock = [1000.0]
        bucket = TokenBucket(capacity=2, refill_per_second=1.0, now=lambda: clock[0])

        assert bucket.allow() is True
        assert bucket.allow() is True
        assert bucket.allow() is False

        clock[0] += 2.0  # two tokens refilled
        assert bucket.allow() is True
        assert bucket.allow() is True
        assert bucket.allow() is False

    def test_refill_capped_at_capacity(self) -> None:
        clock = [1000.0]
        bucket = TokenBucket(capacity=2, refill_per_second=1.0, now=lambda: clock[0])

        clock[0] += 100.0  # would refill 100 but cap is 2
        assert [bucket.allow() for _ in range(3)] == [True, True, False]


class TestWebhookRateLimiter:
    def test_buckets_are_per_source(self) -> None:
        clock = [0.0]
        limiter = WebhookRateLimiter(capacity=1, refill_per_second=0.0, now=lambda: clock[0])

        assert limiter.allow("slack") is True
        assert limiter.allow("slack") is False
        # gitlab has its own bucket — unaffected by slack exhaustion
        assert limiter.allow("gitlab") is True

    def test_unknown_source_is_rejected_without_creating_a_bucket(self, caplog) -> None:
        clock = [0.0]
        limiter = WebhookRateLimiter(capacity=10, refill_per_second=0.0, now=lambda: clock[0])

        with caplog.at_level("WARNING", logger="teatree.core.views._rate_limit"):
            assert limiter.allow("bogus-source") is False
            assert limiter.allow("bogus-source") is False

        # No unbounded bucket was created for the unknown source.
        assert "bogus-source" not in limiter._buckets
        assert any("bogus-source" in r.message for r in caplog.records)

    def test_every_known_source_value_is_allowed(self) -> None:
        clock = [0.0]
        limiter = WebhookRateLimiter(capacity=1, refill_per_second=0.0, now=lambda: clock[0])

        for source in IncomingEvent.Source.values:
            assert limiter.allow(source) is True


TOKEN = "test-gitlab-token"


def _gitlab_status(client: Client, n: int) -> int:
    return client.post(
        reverse("teatree:gitlab_webhook"),
        data=json.dumps({"object_kind": "note", "object_attributes": {"iid": n}}).encode(),
        content_type="application/json",
        HTTP_X_GITLAB_TOKEN=TOKEN,
    ).status_code


@override_settings(
    TEATREE_GITLAB_WEBHOOK_TOKEN=TOKEN,
    TEATREE_WEBHOOK_RATE_CAPACITY=2,
    TEATREE_WEBHOOK_RATE_REFILL_PER_SECOND=0.0,
)
class TestGitLabWebhookRateLimited(TestCase):
    # The limiter is reset between tests by the autouse
    # _reset_webhook_rate_limiter fixture in tests/conftest.py.

    def test_storm_is_throttled_with_429(self) -> None:
        statuses = [_gitlab_status(self.client, i) for i in range(4)]

        assert statuses == [200, 200, 429, 429]
        # only the two accepted events were persisted — the storm did not fill the DB
        assert IncomingEvent.objects.count() == 2
