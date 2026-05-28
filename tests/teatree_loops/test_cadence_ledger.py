"""Cadence ledger — :class:`MiniLoopMarker` manager contract.

Covers the two methods the orchestrator depends on:
``mark_fired`` (upsert by name) and ``elapsed_since`` (None on bootstrap,
seconds otherwise).
"""

import datetime as dt

from django.test import TestCase

from teatree.core.models import MiniLoopMarker


class MiniLoopMarkerManagerTests(TestCase):
    def test_elapsed_since_returns_none_on_bootstrap(self) -> None:
        now = dt.datetime(2026, 1, 1, 12, tzinfo=dt.UTC)
        assert MiniLoopMarker.objects.elapsed_since("inbox", now) is None

    def test_mark_fired_creates_row(self) -> None:
        ts = dt.datetime(2026, 1, 1, 12, tzinfo=dt.UTC)
        MiniLoopMarker.objects.mark_fired("inbox", ts)
        assert MiniLoopMarker.objects.filter(name="inbox").exists()

    def test_mark_fired_updates_existing(self) -> None:
        first = dt.datetime(2026, 1, 1, 12, tzinfo=dt.UTC)
        second = dt.datetime(2026, 1, 1, 13, tzinfo=dt.UTC)
        MiniLoopMarker.objects.mark_fired("inbox", first)
        MiniLoopMarker.objects.mark_fired("inbox", second)
        assert MiniLoopMarker.objects.filter(name="inbox").count() == 1
        row = MiniLoopMarker.objects.get(name="inbox")
        assert row.last_fired_at == second

    def test_elapsed_since_returns_seconds(self) -> None:
        fired = dt.datetime(2026, 1, 1, 12, tzinfo=dt.UTC)
        now = dt.datetime(2026, 1, 1, 12, 5, tzinfo=dt.UTC)
        MiniLoopMarker.objects.mark_fired("inbox", fired)
        elapsed = MiniLoopMarker.objects.elapsed_since("inbox", now)
        assert elapsed is not None
        # Delta is exactly 300s (5 minutes) so int comparison is safe.
        assert int(elapsed) == 300

    def test_str_includes_name_and_timestamp(self) -> None:
        ts = dt.datetime(2026, 1, 1, 12, tzinfo=dt.UTC)
        MiniLoopMarker.objects.mark_fired("review", ts)
        row = MiniLoopMarker.objects.get(name="review")
        assert "review" in str(row)
        assert "2026-01-01" in str(row)
