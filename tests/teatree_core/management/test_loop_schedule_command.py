"""``manage.py loop_schedule`` — list/show/set-active/clear-active against a real DB."""

import datetime as dt
import io
import json

import django.test
import pytest
from django.core.management import call_command

from teatree.core.models import ConfigSetting, ModeSchedule, ModeScheduleSlot
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING


def _run(*args: str, **kwargs: object) -> str:
    out = io.StringIO()
    call_command("loop_schedule", *args, stdout=out, **kwargs)
    return out.getvalue()


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestLoopScheduleCommand(django.test.TestCase):
    def _schedule(self, name: str) -> ModeSchedule:
        schedule = ModeSchedule.objects.create(name=name, timezone="UTC")
        ModeScheduleSlot.objects.create(
            schedule=schedule, days=[0, 1, 2, 3, 4], start_time=dt.time(8, 0), preset_name="engaged"
        )
        return schedule

    def test_set_active_writes_config_setting(self) -> None:
        self._schedule("standard")
        _run("set-active", "standard")
        assert ConfigSetting.objects.get_effective(ACTIVE_SCHEDULE_SETTING) == "standard"

    def test_set_active_refuses_unknown_schedule(self) -> None:
        with pytest.raises(SystemExit):
            _run("set-active", "ghost")

    def test_clear_active_removes_the_setting(self) -> None:
        self._schedule("standard")
        _run("set-active", "standard")
        _run("clear-active")
        assert ConfigSetting.objects.get_effective(ACTIVE_SCHEDULE_SETTING) is None

    def test_list_marks_active(self) -> None:
        self._schedule("standard")
        _run("set-active", "standard")
        payload = json.loads(_run("list", json_output=True))
        assert payload["active"] == "standard"
        assert any(row["active"] for row in payload["schedules"])

    def test_show_renders_slots(self) -> None:
        self._schedule("standard")
        payload = json.loads(_run("show", "standard", json_output=True))
        assert payload["slots"][0]["preset"] == "engaged"
        assert payload["slots"][0]["start_time"] == "08:00"
