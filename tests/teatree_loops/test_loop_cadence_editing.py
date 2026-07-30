"""The per-loop cadence write seam — interval XOR wall-clock, validated against bounds (#3559)."""

import datetime as dt

import pytest
from django.test import TestCase

from teatree.core.models import Loop
from teatree.loops.live import build_report
from teatree.loops.loop_cadence_editing import (
    ABSOLUTE_MIN_INTERVAL_SECONDS,
    CadenceEditError,
    cadence_bounds_for,
    set_loop_cadence,
)


def _loop(name: str = "inbox", **kwargs: object) -> Loop:
    """The seeded row for *name*, forced to a known cadence (the default loops ship seeded)."""
    defaults: dict[str, object] = {"script": f"src/teatree/loops/{name}/loop.py", "delay_seconds": 60, "daily_at": None}
    defaults.update(kwargs)
    loop, _ = Loop.objects.update_or_create(name=name, defaults=defaults)
    return loop


class IntervalCadenceTestCase(TestCase):
    def setUp(self) -> None:
        _loop("inbox")

    def test_setting_an_interval_persists(self) -> None:
        set_loop_cadence("inbox", delay_seconds=300)
        assert Loop.objects.get(name="inbox").delay_seconds == 300

    def test_setting_an_interval_is_reflected_by_the_live_status_read_path(self) -> None:
        set_loop_cadence("inbox", delay_seconds=300)
        entry = next(row for row in build_report().mini_loops if row.name == "inbox")
        assert entry.cadence_seconds == 300

    def test_zero_is_refused_and_does_not_persist(self) -> None:
        with pytest.raises(CadenceEditError):
            set_loop_cadence("inbox", delay_seconds=0)
        assert Loop.objects.get(name="inbox").delay_seconds == 60

    def test_negative_is_refused(self) -> None:
        with pytest.raises(CadenceEditError):
            set_loop_cadence("inbox", delay_seconds=-30)

    def test_below_the_absolute_minimum_is_refused_with_a_clear_message(self) -> None:
        with pytest.raises(CadenceEditError, match=str(ABSOLUTE_MIN_INTERVAL_SECONDS)):
            set_loop_cadence("inbox", delay_seconds=ABSOLUTE_MIN_INTERVAL_SECONDS - 1)

    def test_unknown_loop_is_refused(self) -> None:
        with pytest.raises(CadenceEditError):
            set_loop_cadence("ghost", delay_seconds=300)


class DailyCadenceTestCase(TestCase):
    def setUp(self) -> None:
        _loop("news", delay_seconds=3600)

    def test_setting_a_wall_clock_time_persists(self) -> None:
        set_loop_cadence("news", daily_at="08:15")
        assert Loop.objects.get(name="news").daily_at == dt.time(8, 15)

    def test_a_bad_wall_clock_time_is_refused(self) -> None:
        with pytest.raises(CadenceEditError):
            set_loop_cadence("news", daily_at="99:99")
        assert Loop.objects.get(name="news").daily_at is None


class CadenceExclusivityTestCase(TestCase):
    """The loop XOR: a row never carries both an interval and a wall-clock time."""

    def setUp(self) -> None:
        _loop("dream", delay_seconds=3600)

    def test_switching_to_daily_clears_the_interval_side_of_the_read(self) -> None:
        set_loop_cadence("dream", daily_at="03:00")
        row = Loop.objects.get(name="dream")
        assert row.daily_at == dt.time(3, 0)
        assert row.cadence_label == "daily 03:00"

    def test_switching_back_to_an_interval_clears_the_wall_clock_time(self) -> None:
        set_loop_cadence("dream", daily_at="03:00")
        set_loop_cadence("dream", delay_seconds=7200)
        row = Loop.objects.get(name="dream")
        assert row.daily_at is None
        assert row.delay_seconds == 7200

    def test_supplying_both_is_refused(self) -> None:
        with pytest.raises(CadenceEditError):
            set_loop_cadence("dream", delay_seconds=7200, daily_at="03:00")
        assert Loop.objects.get(name="dream").daily_at is None

    def test_supplying_neither_is_refused(self) -> None:
        with pytest.raises(CadenceEditError):
            set_loop_cadence("dream")


class CadenceFloorTestCase(TestCase):
    """A registry-floor loop's outer tick must stay at least as frequent as its declared floor."""

    def setUp(self) -> None:
        _loop("resource_pressure")

    def test_bounds_expose_the_registry_floor(self) -> None:
        bounds = cadence_bounds_for("resource_pressure")
        assert bounds.max_interval_seconds == 60

    def test_a_loop_without_a_declared_floor_has_no_ceiling(self) -> None:
        _loop("review", delay_seconds=300)
        assert cadence_bounds_for("review").max_interval_seconds is None

    def test_slower_than_the_floor_is_refused_and_does_not_persist(self) -> None:
        with pytest.raises(CadenceEditError, match="60"):
            set_loop_cadence("resource_pressure", delay_seconds=3600)
        assert Loop.objects.get(name="resource_pressure").delay_seconds == 60

    def test_a_daily_time_on_a_floor_loop_is_refused(self) -> None:
        with pytest.raises(CadenceEditError):
            set_loop_cadence("resource_pressure", daily_at="03:00")
        assert Loop.objects.get(name="resource_pressure").daily_at is None

    def test_at_the_floor_is_accepted(self) -> None:
        set_loop_cadence("resource_pressure", delay_seconds=60)
        assert Loop.objects.get(name="resource_pressure").delay_seconds == 60
