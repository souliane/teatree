"""Pure formatting-helper selectors.

Split verbatim from the former monolithic ``tests/teatree_core/test_selectors.py`` (souliane/teatree#443).
"""

from django.utils import timezone

from teatree.core.selectors import _humanize_duration, _list_of_str, _uptime_from_epoch_ms


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
