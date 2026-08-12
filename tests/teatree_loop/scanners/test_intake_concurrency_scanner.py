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


def _scan(scanner: IntakeConcurrencyScanner, *, available_mib: int | None, cores: int = 8) -> list[ScanSignal]:
    with (
        patch(
            f"{_MODULE}.read_ram_headroom",
            return_value=RamHeadroom(
                available_mib=available_mib, cgroup_limit_mib=None, host_available_mib=available_mib
            ),
        ),
        patch(f"{_MODULE}.available_cpu_count", return_value=cores),
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
