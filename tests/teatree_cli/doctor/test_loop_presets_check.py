"""``_check_loop_presets`` — the `t3 doctor` dangling-reference warning (#3159)."""

import datetime as dt

import django.test

from teatree.cli.doctor.checks import _check_loop_presets
from teatree.core.models import ConfigSetting, Loop, LoopPreset, LoopPresetOverride, LoopSchedule, LoopScheduleSlot
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestLoopPresetsDoctorCheck(django.test.TestCase):
    def test_clean_tables_pass(self) -> None:
        assert _check_loop_presets() is True

    def test_override_naming_deleted_preset_warns(self) -> None:
        LoopPresetOverride.objects.set_override("ghost")
        assert _check_loop_presets() is False

    def test_slot_naming_deleted_preset_warns(self) -> None:
        schedule = LoopSchedule.objects.create(name="standard", timezone="UTC")
        LoopScheduleSlot.objects.create(schedule=schedule, days=[0], start_time=dt.time(8, 0), preset_name="ghost")
        assert _check_loop_presets() is False

    def test_entry_naming_deleted_loop_warns(self) -> None:
        LoopPreset.objects.create(name="p", entries={"nonexistent_loop": False})
        assert _check_loop_presets() is False

    def test_known_references_pass(self) -> None:
        Loop.objects.create(name="kr-review", delay_seconds=60, script="src/teatree/loops/kr-review/loop.py")
        LoopPreset.objects.create(name="heads-down", entries={"kr-review": False})
        LoopPresetOverride.objects.set_override("heads-down")
        assert _check_loop_presets() is True

    def test_active_schedule_naming_unknown_warns(self) -> None:
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "ghost")
        assert _check_loop_presets() is False
