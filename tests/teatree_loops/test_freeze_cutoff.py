"""``freeze_cutoff_seconds`` — how stale an anchor may get, scaled to the loop's OWN cadence.

The flat 24h cutoff it replaces reported the weekly ``memory_skim`` as frozen for
roughly 86% of every week, next to a genuinely dead daily loop — diluting the signal
the alarm exists to carry (#4355).
"""

from teatree.loops.loop_staleness import FREEZE_ALARM_FLOOR_SECONDS, STALE_CADENCE_MULTIPLIER, freeze_cutoff_seconds

_DAY = 86400
_WEEK = 7 * _DAY
_THREE_DAYS = 3 * _DAY


class TestFreezeCutoff:
    def test_a_weekly_loop_tolerates_three_days(self) -> None:
        assert freeze_cutoff_seconds(_WEEK) > _THREE_DAYS

    def test_a_daily_loop_does_not_tolerate_three_days(self) -> None:
        assert freeze_cutoff_seconds(_DAY) <= _THREE_DAYS

    def test_the_cutoff_scales_with_the_cadence(self) -> None:
        assert freeze_cutoff_seconds(_WEEK) == STALE_CADENCE_MULTIPLIER * _WEEK

    def test_a_fast_loop_keeps_the_day_floor(self) -> None:
        # 3x a minute is 3 minutes; alarming that fast is the noise nobody drains.
        assert freeze_cutoff_seconds(60) == FREEZE_ALARM_FLOOR_SECONDS

    def test_a_cadence_less_loop_keeps_the_day_floor(self) -> None:
        assert freeze_cutoff_seconds(None) == FREEZE_ALARM_FLOOR_SECONDS
