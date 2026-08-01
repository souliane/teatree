"""Adaptive intake concurrency — the resource loop owns the number (#3992).

The number this replaces was chosen once, in advance, for conditions that vary by the
hour: too small on an idle box, too generous when several agents run full suites at
once. So the acceptance pinned first is not "the value is right" but "the value MOVES
with the reading" — an idle box and a loaded box must not resolve to the same limit,
which is exactly what a fixed setting does.

Two properties are load-bearing and each gets its own class: a reserve is kept rather
than sizing to the last gigabyte, and the ramp is asymmetric — down fast, up slowly —
so the cheaper mistake is the one the system makes under uncertainty.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.intake.concurrency import (
    ADAPTIVE_FRESHNESS,
    HARD_CAP_PER_CORE,
    BoxSizing,
    adapt_concurrency,
    resolve_intake_concurrency,
)
from teatree.core.models.config_setting import ConfigSetting
from teatree.core.models.resource_pressure_marker import ResourcePressureMarker

#: The box the issue measured: 8 cores, ~6.2 GB per agent in full verification.
_CORES = 8
_PER_AGENT_GB = 6.2
_RESERVE_GB = 4.0
_HARD_CAP = int(_CORES * HARD_CAP_PER_CORE)


def _adapt(*, per_agent_gb: float = _PER_AGENT_GB, **kwargs: object) -> int | None:
    base: dict[str, object] = {"available_gb": 20.0, "in_flight": 2, "previous": 2}
    sizing = BoxSizing(cores=_CORES, reserve_gb=_RESERVE_GB, per_agent_gb=per_agent_gb)
    return adapt_concurrency(sizing=sizing, **{**base, **kwargs})


def _adapted(*, per_agent_gb: float = _PER_AGENT_GB, **kwargs: object) -> int:
    """*_adapt* for the cases that must produce a number, so a ``None`` fails loudly."""
    value = _adapt(per_agent_gb=per_agent_gb, **kwargs)
    assert value is not None
    return value


class TestAdaptToObservedHeadroom(TestCase):
    """The acceptance criterion: the limit follows the reading, it is not a constant."""

    def test_idle_and_loaded_readings_do_not_resolve_to_the_same_limit(self) -> None:
        idle = _adapted(available_gb=28.0, in_flight=0, previous=2)
        loaded = _adapted(available_gb=2.0, in_flight=3, previous=3)

        assert idle != loaded

    def test_comfortable_headroom_raises_the_limit(self) -> None:
        assert _adapted(available_gb=28.0, in_flight=0, previous=2) == 3

    def test_a_tightening_box_lowers_the_limit_below_what_is_running(self) -> None:
        assert _adapted(available_gb=1.0, in_flight=3, previous=3) < 3

    def test_an_unreadable_probe_yields_no_opinion(self) -> None:
        assert _adapt(available_gb=None) is None


class TestReserve(TestCase):
    """Sizing to the last available gigabyte turns a burst into an OOM."""

    def test_headroom_short_of_the_reserve_plus_one_agent_admits_nothing_extra(self) -> None:
        assert _adapted(available_gb=_RESERVE_GB + _PER_AGENT_GB - 0.1, in_flight=2, previous=2) == 2

    def test_headroom_past_the_reserve_plus_one_agent_admits_one_more(self) -> None:
        assert _adapted(available_gb=_RESERVE_GB + _PER_AGENT_GB + 0.1, in_flight=2, previous=2) == 3

    def test_tightening_lowers_the_limit_while_memory_still_remains(self) -> None:
        # 3.9 GB is still free — the limit drops because that headroom is inside the
        # reserve, which is the whole point: tighten BEFORE the box hits its ceiling.
        assert _adapted(available_gb=3.9, in_flight=2, previous=2) == 1


class TestAsymmetricRamp(TestCase):
    """Move down fast, up slowly: react quickly to tightening, cautiously to slack."""

    def test_a_multi_step_drop_is_adopted_whole(self) -> None:
        assert _adapted(available_gb=0.0, in_flight=1, previous=4) == 1

    def test_a_multi_step_rise_advances_one_step(self) -> None:
        assert _adapted(available_gb=60.0, in_flight=0, previous=1) == 2

    def test_a_rise_never_overshoots_the_target(self) -> None:
        assert _adapted(available_gb=_RESERVE_GB + _PER_AGENT_GB + 0.1, in_flight=0, previous=1) == 1


class TestBounds(TestCase):
    def test_never_drops_below_one(self) -> None:
        assert _adapted(available_gb=0.0, in_flight=0, previous=1) == 1

    def test_a_misread_cannot_exceed_the_core_derived_hard_cap(self) -> None:
        assert _adapted(available_gb=10_000.0, in_flight=99, previous=99) == _HARD_CAP

    def test_a_nonsense_per_agent_cost_yields_no_opinion(self) -> None:
        assert _adapt(per_agent_gb=0.0) is None


class TestResolveAgainstTheLedger(TestCase):
    """The durable reader — every uncertain case keeps the operator's static setting."""

    def _record(self, value: int, *, age: timedelta = timedelta()) -> None:
        marker = ResourcePressureMarker.load()
        marker.record_adaptive_concurrency(value)
        ResourcePressureMarker.objects.filter(pk=marker.pk).update(adaptive_intake_recorded_at=timezone.now() - age)

    def test_a_fresh_reading_replaces_the_static_setting(self) -> None:
        self._record(4)

        assert resolve_intake_concurrency(2) == 4

    def test_a_never_computed_value_keeps_the_static_setting(self) -> None:
        ResourcePressureMarker.load()

        assert resolve_intake_concurrency(2) == 2

    def test_an_absent_row_keeps_the_static_setting(self) -> None:
        assert resolve_intake_concurrency(2) == 2

    def test_an_unreadable_ledger_keeps_the_static_setting(self) -> None:
        with patch.object(ResourcePressureMarker.objects, "filter", side_effect=RuntimeError("db gone")):
            assert resolve_intake_concurrency(2) == 2

    def test_a_stale_reading_keeps_the_static_setting(self) -> None:
        self._record(4, age=ADAPTIVE_FRESHNESS + timedelta(minutes=1))

        assert resolve_intake_concurrency(2) == 2

    def test_the_kill_switch_keeps_the_static_setting(self) -> None:
        self._record(4)
        ConfigSetting.objects.set_value("adaptive_intake_concurrency_enabled", value=False)

        assert resolve_intake_concurrency(2) == 2
