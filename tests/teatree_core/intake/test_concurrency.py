"""Adaptive intake concurrency — the resource loop owns the number (#3992).

The number this replaces was chosen once, in advance, for conditions that vary by the
hour: too small on an idle box, too generous when several agents run full suites at
once. So the acceptance pinned first is not "the value is right" but "the value MOVES
with the reading" — an idle box and a loaded box must not resolve to the same limit,
which is exactly what a fixed setting does.

Three properties are load-bearing and each gets its own class: a reserve is kept rather
than sizing to the last gigabyte, the ramp is asymmetric — down fast, up slowly — so the
cheaper mistake is the one the system makes under uncertainty, and the reading is
WHOLE-BOX rather than the factory's own count (#4407) — anything else running on the
machine consumes the same cores and appears in none of the factory's ledgers.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.admission_governor import box_load_headroom
from teatree.core.intake.concurrency import (
    ADAPTIVE_FRESHNESS,
    HARD_CAP_PER_CORE,
    MIN_CONCURRENCY,
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
    # An IDLE box by default (headroom 1.0), so the memory-shaped cases below read the
    # memory term alone and the occupancy cases opt in explicitly.
    base: dict[str, object] = {
        "available_gb": 20.0,
        "factory_in_flight": 2,
        "admission_headroom": 1.0,
        "previous": 2,
    }
    sizing = BoxSizing(cores=_CORES, reserve_gb=_RESERVE_GB, per_agent_gb=per_agent_gb)
    return adapt_concurrency(sizing=sizing, **{**base, **kwargs})


#: Memory plentiful and the factory holding nothing — every LANE-LOCAL count reads idle,
#: so only a whole-box term can lower the number this produces.
_ROOMY: dict[str, object] = {"available_gb": 60.0, "factory_in_flight": 0, "previous": _HARD_CAP}


def _adapted(*, per_agent_gb: float = _PER_AGENT_GB, **kwargs: object) -> int:
    """*_adapt* for the cases that must produce a number, so a ``None`` fails loudly."""
    value = _adapt(per_agent_gb=per_agent_gb, **kwargs)
    assert value is not None
    return value


class TestAdaptToObservedHeadroom(TestCase):
    """The acceptance criterion: the limit follows the reading, it is not a constant."""

    def test_idle_and_loaded_readings_do_not_resolve_to_the_same_limit(self) -> None:
        idle = _adapted(available_gb=28.0, factory_in_flight=0, previous=2)
        loaded = _adapted(available_gb=2.0, factory_in_flight=3, previous=3)

        assert idle != loaded

    def test_comfortable_headroom_raises_the_limit(self) -> None:
        assert _adapted(available_gb=28.0, factory_in_flight=0, previous=2) == 3

    def test_a_tightening_box_lowers_the_limit_below_what_is_running(self) -> None:
        assert _adapted(available_gb=1.0, factory_in_flight=3, previous=3) < 3

    def test_an_unreadable_probe_yields_no_opinion(self) -> None:
        assert _adapt(available_gb=None) is None


class TestReserve(TestCase):
    """Sizing to the last available gigabyte turns a burst into an OOM."""

    def test_headroom_short_of_the_reserve_plus_one_agent_admits_nothing_extra(self) -> None:
        assert _adapted(available_gb=_RESERVE_GB + _PER_AGENT_GB - 0.1, factory_in_flight=2, previous=2) == 2

    def test_headroom_past_the_reserve_plus_one_agent_admits_one_more(self) -> None:
        assert _adapted(available_gb=_RESERVE_GB + _PER_AGENT_GB + 0.1, factory_in_flight=2, previous=2) == 3

    def test_tightening_lowers_the_limit_while_memory_still_remains(self) -> None:
        # 3.9 GB is still free — the limit drops because that headroom is inside the
        # reserve, which is the whole point: tighten BEFORE the box hits its ceiling.
        assert _adapted(available_gb=3.9, factory_in_flight=2, previous=2) == 1


class TestAsymmetricRamp(TestCase):
    """Move down fast, up slowly: react quickly to tightening, cautiously to slack."""

    def test_a_multi_step_drop_is_adopted_whole(self) -> None:
        assert _adapted(available_gb=0.0, factory_in_flight=1, previous=4) == 1

    def test_a_multi_step_rise_advances_one_step(self) -> None:
        assert _adapted(available_gb=60.0, factory_in_flight=0, previous=1) == 2

    def test_a_rise_never_overshoots_the_target(self) -> None:
        assert _adapted(available_gb=_RESERVE_GB + _PER_AGENT_GB + 0.1, factory_in_flight=0, previous=1) == 1


class TestWholeBoxOccupancy(TestCase):
    """The reading is the BOX, not the factory's own count (#4407).

    The recorded incident: the factory correctly held intake at 3 while a parallel
    fan-out took load from 14 to 53. Those processes claim no task and hold no intake
    marker, so every count the factory keeps read healthy while it kept admitting.
    """

    def _at_load(self, load1: float, **kwargs: object) -> int:
        headroom = box_load_headroom(load1=load1, cores=_CORES)
        return _adapted(admission_headroom=headroom, **{**_ROOMY, **kwargs})

    def test_load_the_factory_did_not_start_lowers_the_limit_memory_alone_would_allow(self) -> None:
        assert self._at_load(30.0) < self._at_load(0.0)

    def test_the_limit_falls_progressively_as_the_box_fills(self) -> None:
        assert self._at_load(0.0) > self._at_load(20.0) > self._at_load(53.0)

    def test_a_saturated_box_never_wedges_intake_to_zero(self) -> None:
        assert self._at_load(53.0) == MIN_CONCURRENCY

    def test_an_unreadable_load_leaves_the_memory_answer_standing(self) -> None:
        assert _adapted(admission_headroom=None, **_ROOMY) == _HARD_CAP


class TestBounds(TestCase):
    def test_never_drops_below_one(self) -> None:
        assert _adapted(available_gb=0.0, factory_in_flight=0, previous=1) == 1

    def test_a_misread_cannot_exceed_the_core_derived_hard_cap(self) -> None:
        assert _adapted(available_gb=10_000.0, factory_in_flight=99, previous=99) == _HARD_CAP

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
