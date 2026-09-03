"""The disk-pressure reading of a freeing payload, and what a pass yield is worth saying.

Pure over values the payload already carries — no filesystem, no DB — so the marker
is a stub and these are unit tests. What they pin is the reading, not the policy
(:mod:`teatree.core.cleanup.reclaim_pressure` has its own suite).
"""

import pytest

from teatree.core.cleanup.reclaim_pressure import ZERO_YIELD_ALARM_PASSES
from teatree.loop.reclaim_yield import pressure_idle_days, reclaim_yield_steps

_WARN = 20.0
_CRIT = 5.0
_IDLE = 2.0


class _MarkerWriteError(RuntimeError):
    """A marker write failing the way a DB error would."""


class _Marker:
    def __init__(self, streak: int) -> None:
        self.streak = streak
        self.seen: list[tuple[float, bool]] = []

    def record_reclaim_pass(self, *, freed_gb: float, under_critical: bool) -> int:
        self.seen.append((freed_gb, under_critical))
        return self.streak


class _RaisingMarker:
    def record_reclaim_pass(self, *, freed_gb: float, under_critical: bool) -> int:
        raise _MarkerWriteError


class TestPressureIdleDays:
    def test_a_comfortable_disk_keeps_the_configured_dormancy(self) -> None:
        payload = {"free_gb": 100.0, "disk_warn_free_gb": _WARN, "disk_crit_free_gb": _CRIT, "venv_idle_days": _IDLE}
        assert pressure_idle_days(payload) == pytest.approx(_IDLE)

    def test_below_the_critical_floor_dormancy_stops_gating(self) -> None:
        payload = {"free_gb": 1.0, "disk_warn_free_gb": _WARN, "disk_crit_free_gb": _CRIT, "venv_idle_days": _IDLE}
        assert pressure_idle_days(payload) is None

    def test_a_threshold_the_payload_omits_never_relaxes(self) -> None:
        assert pressure_idle_days({"free_gb": 1.0, "venv_idle_days": _IDLE}) == pytest.approx(_IDLE)


class TestReclaimYieldSteps:
    def test_a_pass_reports_its_yield_and_the_streak(self) -> None:
        marker = _Marker(streak=0)
        steps = reclaim_yield_steps(marker, reclaimed_gb=1.5, payload={"free_gb": 100.0, "disk_crit_free_gb": _CRIT})
        assert steps == ["YIELD 1.50 GB this pass (zero-yield streak 0)"]
        assert marker.seen == [(1.5, False)]

    def test_a_run_of_empty_passes_below_the_floor_raises_the_stall_alarm(self) -> None:
        marker = _Marker(streak=ZERO_YIELD_ALARM_PASSES)
        steps = reclaim_yield_steps(marker, reclaimed_gb=0.0, payload={"free_gb": 1.0, "disk_crit_free_gb": _CRIT})
        assert len(steps) == 2
        assert "STALLED disk reclaim" in steps[1]
        assert marker.seen == [(0.0, True)]

    def test_the_same_streak_above_the_floor_is_not_an_alarm(self) -> None:
        marker = _Marker(streak=ZERO_YIELD_ALARM_PASSES)
        steps = reclaim_yield_steps(marker, reclaimed_gb=0.0, payload={"free_gb": 100.0, "disk_crit_free_gb": _CRIT})
        assert len(steps) == 1

    def test_a_failed_marker_write_costs_the_steps_not_the_pass(self) -> None:
        assert reclaim_yield_steps(_RaisingMarker(), reclaimed_gb=0.0, payload={"free_gb": 1.0}) == []
