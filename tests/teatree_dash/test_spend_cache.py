"""The health page's spend aggregate is cached, not recomputed per poll (#3674 F16).

``_spend_summary`` runs a cycle-to-date ``TaskAttempt`` aggregate. The health page
polls every ~5s; recomputing the full-table scan on every poll is the documented
offender. A short-TTL cache makes the second poll within the window issue zero
aggregate queries.
"""

from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from teatree.dash import health_bands
from tests.factories import TaskAttemptFactory, TaskFactory, TicketFactory


class SpendSummaryCacheTestCase(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.addCleanup(cache.clear)
        ticket = TicketFactory()
        for _ in range(5):
            TaskAttemptFactory(task=TaskFactory(ticket=ticket), model="claude")

    def test_first_call_computes_second_call_hits_cache(self) -> None:
        with CaptureQueriesContext(connection) as first:
            health_bands._spend_summary()
        assert len(first) > 0, "first call must compute the aggregate"

        with CaptureQueriesContext(connection) as second:
            health_bands._spend_summary()
        assert len(second) == 0, f"cached poll must issue zero queries, got {len(second)}"

    def test_cache_expiry_recomputes(self) -> None:
        health_bands._spend_summary()
        cache.delete(health_bands._SPEND_CACHE_KEY)
        with CaptureQueriesContext(connection) as after:
            health_bands._spend_summary()
        assert len(after) > 0, "a cleared cache must recompute"

    def test_none_result_is_cached_too(self) -> None:
        # A broken cost read fails open to None; that None must be cached so a
        # broken read is not re-hammered on every poll.
        with patch.object(health_bands, "CostReport") as report:
            report.build.side_effect = RuntimeError("boom")
            assert health_bands._spend_summary() is None
            with CaptureQueriesContext(connection) as second:
                assert health_bands._spend_summary() is None
        assert len(second) == 0
