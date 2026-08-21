"""The resource loop's intake-sizing job — measure, decide, persist, surface (#3992).

The scanner is the wiring half; the arithmetic is pinned in
``tests/teatree_core/intake/test_concurrency.py``. What matters here is that the
adjustment is DURABLE (intake reads a row, not a return value), that it advances once
per measurement window rather than once per tick, and that it is VISIBLE when the number
moves — an adjustment nobody can see is indistinguishable from a knob nobody turned.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.admission_governor import MachineSignal, QuotaSignal
from teatree.core.models.resource_pressure_marker import ResourcePressureMarker
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.intake_concurrency import IntakeConcurrencyScanner
from teatree.utils.ram_scope import RamHeadroom

_MODULE = "teatree.loop.scanners.intake_concurrency"


def _scanner(**overrides: object) -> IntakeConcurrencyScanner:
    base: dict[str, object] = {
        "static_ceiling": 2,
        "reserve_gb": 4.0,
        "per_agent_gb": 6.2,
        "cadence_minutes": 5,
    }
    return IntakeConcurrencyScanner(**{**base, **overrides})


_WEEK = 7 * 24 * 3600


def _quota(weekly: float = 0.0) -> QuotaSignal:
    return QuotaSignal(
        fresh=True,
        all_accounts_exhausted=False,
        weekly_utilization=weekly,
        short_utilization=0.0,
        # Half a week left, so a spent window reads as a genuine runway problem.
        seconds_to_weekly_reset=_WEEK * 0.5,
    )


def _scan(
    scanner: IntakeConcurrencyScanner,
    *,
    available_mib: int | None,
    cores: int = 8,
    load1: float = 0.0,
    weekly: float = 0.0,
) -> list[ScanSignal]:
    """One window against a stated box.

    Every reading is pinned: the ambient load average and the account quota cache are
    live properties of the host, so leaving either unstubbed would make the cases below
    pass or fail on whatever else the machine happens to be doing.
    """
    with (
        patch(
            f"{_MODULE}.read_ram_headroom",
            return_value=RamHeadroom(
                available_mib=available_mib, cgroup_limit_mib=None, host_available_mib=available_mib
            ),
        ),
        patch(f"{_MODULE}.available_cpu_count", return_value=cores),
        patch(
            f"{_MODULE}.read_machine_signal",
            return_value=MachineSignal(cores=cores, load1=load1, ram_available_gb=None),
        ),
        patch(f"{_MODULE}.read_quota_signal", return_value=_quota(weekly)),
    ):
        return list(scanner.scan())


def _idle_mib(gb: float = 30.0) -> int:
    return int(gb * 1024)


class TestPersistsTheDecision(TestCase):
    def test_an_idle_box_raises_the_number_off_the_static_ceiling(self) -> None:
        _scan(_scanner(), available_mib=_idle_mib())

        assert ResourcePressureMarker.load().adaptive_intake_concurrency == 3

    def test_the_first_pass_ramps_from_the_static_ceiling_not_from_zero(self) -> None:
        # The operator's number is what intake was actually using, so it is the honest
        # seed: a cold start steps away from it rather than jumping to the box maximum.
        _scan(_scanner(static_ceiling=1), available_mib=_idle_mib(60.0))

        assert ResourcePressureMarker.load().adaptive_intake_concurrency == 2

    def test_a_tightening_box_lowers_the_persisted_number(self) -> None:
        scanner = _scanner(static_ceiling=3)
        _scan(scanner, available_mib=(2 * 1024))

        assert ResourcePressureMarker.load().adaptive_intake_concurrency < 3

    def test_an_unreadable_probe_writes_nothing(self) -> None:
        assert _scan(_scanner(), available_mib=None) == []
        assert ResourcePressureMarker.load().adaptive_intake_concurrency is None

    def test_a_zero_per_agent_cost_writes_nothing(self) -> None:
        assert _scan(_scanner(per_agent_gb=0.0), available_mib=_idle_mib()) == []
        assert ResourcePressureMarker.load().adaptive_intake_concurrency is None


class TestSuppliesTheWholeBoxReading(TestCase):
    """The scanner's job is both signals, not memory alone (#4407).

    The arithmetic is pinned in ``tests/teatree_core/intake/test_concurrency.py``; what
    matters here is that the live load average actually REACHES it — a term the decision
    accepts and nothing ever supplies is the same blindness with more code.
    """

    def test_the_same_memory_reading_sizes_lower_on_a_saturated_box(self) -> None:
        _scan(_scanner(static_ceiling=3), available_mib=_idle_mib(), load1=53.0)
        saturated = ResourcePressureMarker.load().adaptive_intake_concurrency

        ResourcePressureMarker.objects.all().delete()
        _scan(_scanner(static_ceiling=3), available_mib=_idle_mib(), load1=0.0)

        assert saturated is not None
        assert saturated < (ResourcePressureMarker.load().adaptive_intake_concurrency or 0)


class TestIntakeSeesTheTokenRunwayNotOnlyTheBox(TestCase):
    """#4508 — the gap the issue names: unloaded by CPU and simultaneously out of runway.

    Intake's two inputs were memory and LOAD, both properties of the machine, so a box
    with idle cores and free RAM kept claiming new issues against a weekly window that
    was nearly gone. Feeding it ``1 - pressure`` instead of the load headroom alone adds
    the quota dimension without changing a single CPU-bound answer.
    """

    def _sized(self, *, weekly: float, load1: float = 0.0) -> int:
        ResourcePressureMarker.objects.all().delete()
        _scan(_scanner(static_ceiling=3), available_mib=_idle_mib(60.0), load1=load1, weekly=weekly)
        sized = ResourcePressureMarker.load().adaptive_intake_concurrency
        assert sized is not None
        return sized

    def test_a_spent_weekly_window_lowers_intake_on_an_otherwise_idle_box(self) -> None:
        assert self._sized(weekly=0.85) < self._sized(weekly=0.0)

    def test_a_healthy_window_leaves_the_cpu_bound_answer_untouched(self) -> None:
        """The generalisation must be inert where load already dominated."""
        assert self._sized(weekly=0.0, load1=30.0) == self._sized(weekly=0.05, load1=30.0)

    def test_the_signal_names_the_load_it_decided_against(self) -> None:
        signal = _scan(_scanner(), available_mib=_idle_mib(), load1=2.5)[0]

        assert signal.payload["load1"] == pytest.approx(2.5)
        assert signal.payload["cores"] == 8
        assert "box load 2.5 on 8 cores" in signal.summary


class TestNeverCrashesTheTick(TestCase):
    """A sizing job that dies takes the whole resource loop's tick with it."""

    def test_an_unloadable_marker_is_survived(self) -> None:
        with patch.object(ResourcePressureMarker, "load", side_effect=RuntimeError("db gone")):
            assert _scan(_scanner(), available_mib=_idle_mib()) == []

    def test_a_failed_write_is_survived(self) -> None:
        with patch.object(ResourcePressureMarker, "record_adaptive_concurrency", side_effect=RuntimeError("locked")):
            assert _scan(_scanner(), available_mib=_idle_mib()) == []


class TestVisibility(TestCase):
    def test_a_moved_number_is_surfaced(self) -> None:
        signals = _scan(_scanner(), available_mib=_idle_mib())

        assert [signal.kind for signal in signals] == ["resource.intake_concurrency_adapted"]

    def test_the_signal_carries_the_reading_it_decided_from(self) -> None:
        signal = _scan(_scanner(), available_mib=_idle_mib())[0]

        assert signal.payload["concurrency"] == 3
        assert signal.payload["previous"] == 2
        assert signal.payload["reserve_gb"] == pytest.approx(4.0)

    def test_an_unchanged_number_is_not_re_announced(self) -> None:
        marker = ResourcePressureMarker.load()
        marker.record_adaptive_concurrency(3)
        _age(marker, timedelta(minutes=10))

        # 25 GB free is exactly what 3 already-sized agents plus the reserve occupy.
        assert _scan(_scanner(), available_mib=_idle_mib(25.0)) == []


class TestCadence(TestCase):
    def test_a_second_pass_inside_the_window_changes_nothing(self) -> None:
        scanner = _scanner()
        _scan(scanner, available_mib=_idle_mib())

        assert _scan(scanner, available_mib=_idle_mib(60.0)) == []
        assert ResourcePressureMarker.load().adaptive_intake_concurrency == 3

    def test_the_next_window_advances_the_ramp_one_step(self) -> None:
        scanner = _scanner()
        _scan(scanner, available_mib=_idle_mib(60.0))
        _age(ResourcePressureMarker.load(), timedelta(minutes=10))

        _scan(scanner, available_mib=_idle_mib(60.0))

        assert ResourcePressureMarker.load().adaptive_intake_concurrency == 4


def _age(marker: ResourcePressureMarker, age: timedelta) -> None:
    ResourcePressureMarker.objects.filter(pk=marker.pk).update(adaptive_intake_recorded_at=timezone.now() - age)
