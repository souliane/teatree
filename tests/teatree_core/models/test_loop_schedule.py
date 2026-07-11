"""teatree.core.models.loop_schedule — schedule + slot shape (#3159)."""

import datetime as dt

import django.test

from teatree.core.models import LoopSchedule, LoopScheduleSlot


class TestLoopScheduleSlotWeekdays(django.test.SimpleTestCase):
    def test_weekdays_keeps_valid_ints(self) -> None:
        slot = LoopScheduleSlot(days=[0, 6, 7, -1, "x"], start_time=dt.time(8, 0), preset_name="engaged")
        assert slot.weekdays == {0, 6}

    def test_weekdays_empty_for_non_list(self) -> None:
        assert LoopScheduleSlot(days="mon", start_time=dt.time(8, 0), preset_name="p").weekdays == set()


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestLoopSchedulePersistence(django.test.TestCase):
    def test_cascade_deletes_slots(self) -> None:
        schedule = LoopSchedule.objects.create(name="standard", timezone="UTC")
        LoopScheduleSlot.objects.create(schedule=schedule, days=[0], start_time=dt.time(8, 0), preset_name="engaged")
        schedule.delete()
        assert LoopScheduleSlot.objects.count() == 0
