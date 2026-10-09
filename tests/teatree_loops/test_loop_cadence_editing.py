"""The per-loop cadence write seam — interval XOR wall-clock, validated against bounds (#3559)."""

import datetime as dt

import pytest
from django.test import TestCase

from teatree.core.models import Loop, Prompt
from teatree.loops.live import build_report
from teatree.loops.loop_cadence_editing import (
    ABSOLUTE_MIN_INTERVAL_SECONDS,
    CadenceEditError,
    cadence_bounds_for,
    off_grid_cadences,
    set_loop_cadence,
)
from teatree.loops.timer_chains import IDLE_POLL_FLOOR_SECONDS


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


class TheFloorIsDerivedFromTheTickTestCase(TestCase):
    """#4079: the minimum interval is the timer chain's own poll floor, not an invented number.

    A loop rides a self-rescheduling timer chain whose successor is held at
    ``now + IDLE_POLL_FLOOR_SECONDS`` on every path that does not complete a clean tick
    (a held/not-due loop, a faulted tick whose anchor never moved, a cadence-less loop).
    So a cadence below that floor is honoured only while nothing goes wrong, and the
    number in the editor is not the number the operator observes.
    """

    def test_the_floor_is_the_timer_chains_poll_floor(self) -> None:
        # Derived, not re-typed: this fails the moment the two drift apart, which is the
        # whole point — the old 30 was a sanity gate that matched nothing in the machinery.
        assert ABSOLUTE_MIN_INTERVAL_SECONDS == IDLE_POLL_FLOOR_SECONDS

    def test_an_interval_below_the_floor_is_refused(self) -> None:
        _loop("inbox")
        with pytest.raises(CadenceEditError):
            set_loop_cadence("inbox", delay_seconds=IDLE_POLL_FLOOR_SECONDS - 1)

    def test_a_non_multiple_of_the_floor_is_refused(self) -> None:
        # 90s reads as 90s but behaves as 60s or 120s depending on whether the last tick
        # completed. Refusing it is what makes the configured number equal the observed one.
        _loop("inbox")
        with pytest.raises(CadenceEditError):
            set_loop_cadence("inbox", delay_seconds=90)

    def test_the_refusal_names_the_admissible_values_on_either_side(self) -> None:
        _loop("inbox")
        with pytest.raises(CadenceEditError) as exc:
            set_loop_cadence("inbox", delay_seconds=90)
        assert "60" in str(exc.value)
        assert "120" in str(exc.value)

    def test_a_multiple_of_the_floor_is_accepted(self) -> None:
        _loop("inbox")
        set_loop_cadence("inbox", delay_seconds=300)
        assert Loop.objects.get(name="inbox").delay_seconds == 300

    def test_a_refused_write_does_not_persist(self) -> None:
        _loop("inbox", delay_seconds=60)
        with pytest.raises(CadenceEditError):
            set_loop_cadence("inbox", delay_seconds=90)
        assert Loop.objects.get(name="inbox").delay_seconds == 60


class ExistingNonMultipleRowsAreReportedNotRewrittenTestCase(TestCase):
    """#4079: an operator who typed 45 meant something — report it, never silently round it."""

    def test_a_stored_non_multiple_row_is_reported(self) -> None:
        _loop("inbox", delay_seconds=45)
        reported = dict(off_grid_cadences())
        assert reported["inbox"] == 45

    def test_a_stored_multiple_row_is_not_reported(self) -> None:
        Loop.objects.all().delete()
        _loop("inbox", delay_seconds=300)
        assert off_grid_cadences() == ()

    def test_reporting_does_not_rewrite_the_row(self) -> None:
        # The guarantee that makes this safe to run anywhere: it is a read.
        _loop("inbox", delay_seconds=45)
        off_grid_cadences()
        assert Loop.objects.get(name="inbox").delay_seconds == 45

    def test_a_daily_row_carries_no_interval_and_is_not_reported(self) -> None:
        # A wall-clock row has no interval at all, so it is outside the grid's scope rather
        # than off the grid. (It is a PROMPT loop: `loop_script_requires_delay` refuses a
        # script loop with no interval.)
        Loop.objects.all().delete()
        prompt, _ = Prompt.objects.get_or_create(name="daily-demo", defaults={"body": "x"})
        Loop.objects.create(name="dream", prompt=prompt, script="", delay_seconds=None, daily_at=dt.time(3, 0))
        assert off_grid_cadences() == ()


class TheBoundsNoteIsNotRepeatedPerRowTestCase(TestCase):
    """#4079: the global floor is a legend, not a sentence on every line.

    Owner: "don't repeat this message on each row it's useless". The floor is the same for
    every ordinary loop, so a per-row note carrying it says nothing about that row. Only a
    ``cadence_is_floor`` loop's registry MAXIMUM is genuinely row-specific.
    """

    def test_an_ordinary_loop_carries_no_per_row_note(self) -> None:
        assert cadence_bounds_for("inbox").note == ""

    def test_a_cadence_is_floor_loop_still_explains_its_own_maximum(self) -> None:
        bounds = cadence_bounds_for("resource_pressure")
        assert bounds.max_interval_seconds is not None
        assert bounds.note != ""
        assert str(bounds.max_interval_seconds) in bounds.note
