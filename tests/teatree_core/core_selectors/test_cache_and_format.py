"""Cache primitives and pure formatting-helper selectors.

Split verbatim from the former monolithic ``tests/teatree_core/test_selectors.py`` (souliane/teatree#443).
"""

import time

from django.utils import timezone

from teatree.core.selectors import (
    _cached,
    _humanize_duration,
    _list_of_str,
    _panel_cache,
    _uptime_from_epoch_ms,
    invalidate_panel_cache,
)


class TestCached:
    def test_returns_stored_value_within_ttl(self) -> None:
        _panel_cache.clear()
        calls: list[int] = []

        def builder() -> str:
            calls.append(1)
            return "fresh"

        assert _cached("test_key", builder, ttl=60.0) == "fresh"
        assert _cached("test_key", builder, ttl=60.0) == "fresh"
        assert len(calls) == 1
        _panel_cache.clear()

    def test_rebuilds_after_ttl_expires(self) -> None:
        _panel_cache.clear()
        calls: list[int] = []

        def builder() -> str:
            calls.append(1)
            return f"v{len(calls)}"

        # Populate cache with a stale entry (timestamp far in the past)
        _panel_cache["stale_key"] = (time.monotonic() - 100, "old")
        result = _cached("stale_key", builder, ttl=1.0)
        assert result == "v1"
        assert len(calls) == 1
        _panel_cache.clear()


class TestInvalidatePanelCache:
    def test_by_name(self) -> None:
        _panel_cache["a"] = (0.0, "val_a")
        _panel_cache["b"] = (0.0, "val_b")

        invalidate_panel_cache("a")

        assert "a" not in _panel_cache
        assert "b" in _panel_cache
        _panel_cache.clear()


class TestHumanizeDuration:
    def test_seconds_only(self) -> None:
        assert _humanize_duration(45) == "45s"

    def test_minutes_and_seconds(self) -> None:
        assert _humanize_duration(150) == "2m 30s"

    def test_exact_minutes(self) -> None:
        assert _humanize_duration(120) == "2m"

    def test_hours_and_minutes(self) -> None:
        assert _humanize_duration(3900) == "1h 5m"

    def test_exact_hours(self) -> None:
        assert _humanize_duration(3600) == "1h"

    def test_zero(self) -> None:
        assert _humanize_duration(0) == "0s"

    def test_negative_clamps_to_zero(self) -> None:
        assert _humanize_duration(-5) == "0s"


class TestUptimeFromEpochMs:
    def test_minutes(self) -> None:
        now_ms = int(timezone.now().timestamp() * 1000)
        # 5 minutes ago
        assert _uptime_from_epoch_ms(now_ms - 5 * 60_000) == "5m"

    def test_hours(self) -> None:
        now_ms = int(timezone.now().timestamp() * 1000)
        # 2 hours and 30 minutes ago
        assert _uptime_from_epoch_ms(now_ms - (2 * 60 + 30) * 60_000) == "2h30m"


class TestListOfStr:
    def test_returns_empty_for_non_list(self) -> None:
        assert _list_of_str("not-a-list") == []

    def test_converts_elements(self) -> None:
        assert _list_of_str([1, "two", 3]) == ["1", "two", "3"]
