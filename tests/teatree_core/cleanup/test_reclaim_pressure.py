"""How disk pressure scales the reclaim criterion, and when a stalled reclaim is an alarm (#4644).

Pure policy over values that already exist — no filesystem, no process table, no
DB — so these are unit tests. The behaviour they pin is the one the evictor is
blind to today: at 2 idle days a checkout rewritten more often than that can
never age into eligibility, whatever the disk is doing.
"""

import pytest

from teatree.core.cleanup.reclaim_pressure import ZERO_YIELD_ALARM_PASSES, effective_idle_days, reclaim_is_stalled

_WARN = 25.0
_CRIT = 10.0
_IDLE = 2.0


class TestEffectiveIdleDays:
    def test_above_the_warn_threshold_the_criterion_is_unchanged(self) -> None:
        assert effective_idle_days(free_gb=200.0, warn_gb=_WARN, crit_gb=_CRIT, idle_days=_IDLE) == _IDLE

    def test_at_the_warn_threshold_the_criterion_is_unchanged(self) -> None:
        assert effective_idle_days(free_gb=_WARN, warn_gb=_WARN, crit_gb=_CRIT, idle_days=_IDLE) == _IDLE

    def test_below_the_critical_floor_dormancy_stops_gating(self) -> None:
        """``None``, not a cutoff of now: a cutoff loses the race with a mid-pass write."""
        assert effective_idle_days(free_gb=0.2, warn_gb=_WARN, crit_gb=_CRIT, idle_days=_IDLE) is None

    def test_between_the_thresholds_the_requirement_decays_linearly(self) -> None:
        assert effective_idle_days(free_gb=17.5, warn_gb=_WARN, crit_gb=_CRIT, idle_days=_IDLE) == pytest.approx(1.0)

    @pytest.mark.parametrize("free_gb", [None, 17.5])
    def test_an_unmeasurable_threshold_never_relaxes(self, free_gb: float | None) -> None:
        assert effective_idle_days(free_gb=free_gb, warn_gb=None, crit_gb=_CRIT, idle_days=_IDLE) == _IDLE
        assert effective_idle_days(free_gb=free_gb, warn_gb=_WARN, crit_gb=None, idle_days=_IDLE) == _IDLE

    def test_an_unmeasurable_free_space_never_relaxes(self) -> None:
        assert effective_idle_days(free_gb=None, warn_gb=_WARN, crit_gb=_CRIT, idle_days=_IDLE) == _IDLE

    @pytest.mark.parametrize("warn_gb", [_CRIT, 5.0])
    def test_a_degenerate_threshold_pair_never_relaxes(self, warn_gb: float) -> None:
        assert effective_idle_days(free_gb=7.0, warn_gb=warn_gb, crit_gb=_CRIT, idle_days=_IDLE) == _IDLE


class TestReclaimIsStalled:
    @pytest.mark.parametrize(
        ("streak", "free_gb", "stalled"),
        [
            (ZERO_YIELD_ALARM_PASSES, 0.2, True),
            (ZERO_YIELD_ALARM_PASSES + 5, 0.2, True),
            (ZERO_YIELD_ALARM_PASSES - 1, 0.2, False),
            (ZERO_YIELD_ALARM_PASSES, 200.0, False),
            (ZERO_YIELD_ALARM_PASSES, None, False),
        ],
    )
    def test_the_stall_predicate_needs_both_the_streak_and_the_pressure(
        self, streak: int, free_gb: float | None, *, stalled: bool
    ) -> None:
        assert reclaim_is_stalled(streak=streak, free_gb=free_gb, crit_gb=_CRIT) is stalled

    def test_an_unknown_floor_proves_no_pressure(self) -> None:
        assert reclaim_is_stalled(streak=ZERO_YIELD_ALARM_PASSES, free_gb=0.2, crit_gb=None) is False
