import datetime as dt

import django.test
from typer.testing import CliRunner, Result

from teatree.cli.loop import loop_app
from teatree.core.models import Mode, ModeSchedule, ModeScheduleSlot


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(loop_app, ["schedule", *args])


class TestScheduleSlotCommands(django.test.TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.schedule = ModeSchedule.objects.create(name="standard", timezone="Europe/Vienna")
        Mode.objects.create(name="present", entries={})
        Mode.objects.create(name="afk", entries={})

    def test_set_slot_upserts_a_real_schedule_slot(self) -> None:
        slot = ModeScheduleSlot.objects.create(
            schedule=self.schedule,
            days=[0, 1, 2, 3, 4],
            start_time=dt.time(19),
            preset_name="maintenance",
        )

        result = _invoke(
            "set-slot",
            "standard",
            "0,1,2,3,4",
            "19:00",
            "afk",
            "--slot-id",
            str(slot.pk),
        )

        slot.refresh_from_db()
        assert result.exit_code == 0, result.output
        assert ModeScheduleSlot.objects.filter(schedule=self.schedule).count() == 1
        assert slot.weekdays == {0, 1, 2, 3, 4}
        assert slot.start_time == dt.time(19)
        assert slot.preset_name == "afk"

    def test_set_slot_refuses_invalid_input(self) -> None:
        result = _invoke("set-slot", "standard", "7", "19:00", "afk")

        assert result.exit_code == 2
        assert "invalid weekdays [7]" in result.output
        assert not ModeScheduleSlot.objects.filter(schedule=self.schedule).exists()

    def test_set_slot_refuses_seconds_without_writing_a_slot(self) -> None:
        result = _invoke("set-slot", "standard", "0", "19:00:30", "afk")

        assert result.exit_code == 2
        assert "invalid start time '19:00:30'; use HH:MM" in result.output
        assert not ModeScheduleSlot.objects.filter(schedule=self.schedule).exists()

    def test_delete_slot_deletes_a_real_schedule_slot(self) -> None:
        slot = ModeScheduleSlot.objects.create(
            schedule=self.schedule,
            days=[0],
            start_time=dt.time(19),
            preset_name="afk",
        )

        result = _invoke("delete-slot", "standard", str(slot.pk))

        assert result.exit_code == 0, result.output
        assert not ModeScheduleSlot.objects.filter(pk=slot.pk).exists()

    def test_delete_slot_refuses_a_slot_from_another_schedule(self) -> None:
        other = ModeSchedule.objects.create(name="holiday", timezone="Europe/Vienna")
        slot = ModeScheduleSlot.objects.create(
            schedule=other,
            days=[0],
            start_time=dt.time(19),
            preset_name="afk",
        )

        result = _invoke("delete-slot", "standard", str(slot.pk))

        assert result.exit_code == 2
        assert f"schedule 'standard' has no slot {slot.pk}" in result.output
        assert ModeScheduleSlot.objects.filter(pk=slot.pk).exists()

    def test_set_slot_then_show_renders_the_changed_calendar(self) -> None:
        set_result = _invoke("set-slot", "standard", "0,1,2,3,4", "19:00", "afk")

        show_result = _invoke("show", "standard")

        slot = ModeScheduleSlot.objects.get(schedule=self.schedule)
        assert set_result.exit_code == 0, set_result.output
        assert show_result.exit_code == 0, show_result.output
        assert f"  [{slot.pk}] Mon,Tue,Wed,Thu,Fri          19:00 -> afk" in show_result.output
