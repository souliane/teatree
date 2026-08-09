"""The shared preset/schedule write seam — tri-state round-trip incl. genuine absence (#3559)."""

import datetime as dt

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import ConfigSetting, Loop, Mode, ModeOverride, ModeSchedule, ModeScheduleSlot
from teatree.core.models.loop_preset import LOW_POWER_PRESET_SETTING
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING
from teatree.loops.enable_verdict import effective_verdicts
from teatree.loops.mode_shape import LOAD_BEARING_LOOPS
from teatree.loops.preset_editing import PresetEditError, activate_preset, clear_preset_override, set_preset_entry
from teatree.loops.schedule_editing import (
    active_schedule_name,
    clear_active_schedule,
    delete_schedule_slot,
    set_active_schedule,
    upsert_schedule_slot,
)


def _loop(name: str, *, enabled: bool = True) -> Loop:
    """The seeded row for *name*, forced to a known state (the default loops ship seeded)."""
    loop, _ = Loop.objects.update_or_create(
        name=name,
        defaults={"script": f"src/teatree/loops/{name}/loop.py", "delay_seconds": 60, "enabled": enabled},
    )
    return loop


def _preset(name: str, entries: dict[str, bool]) -> Mode:
    preset, _ = Mode.objects.update_or_create(name=name, defaults={"entries": entries})
    return preset


def _schedule(name: str) -> ModeSchedule:
    schedule, _ = ModeSchedule.objects.get_or_create(name=name)
    ModeScheduleSlot.objects.filter(schedule=schedule).delete()
    return schedule


class PresetEntryTriStateTestCase(TestCase):
    """``on`` / ``off`` / absent all round-trip — absent must never be stored as ``False``."""

    def setUp(self) -> None:
        _loop("review")
        self.preset = _preset("present", {"review": True})

    def test_setting_off_stores_false(self) -> None:
        set_preset_entry("present", "review", "off")
        assert Mode.objects.by_name("present").entries == {"review": False}

    def test_setting_on_stores_true(self) -> None:
        set_preset_entry("present", "review", "off")
        set_preset_entry("present", "review", "on")
        assert Mode.objects.by_name("present").entries == {"review": True}

    def test_setting_inherit_removes_the_key_entirely(self) -> None:
        # The bug this pins: "no opinion" implemented as False would mask the loop
        # instead of falling through to Loop.enabled.
        set_preset_entry("present", "review", "inherit")
        entries = Mode.objects.by_name("present").entries
        assert "review" not in entries
        assert entries.get("review") is not False

    def test_inherit_leaves_the_tri_state_read_as_none(self) -> None:
        set_preset_entry("present", "review", "inherit")
        assert Mode.objects.by_name("present").state_for("review") is None

    def test_unknown_value_is_refused_and_does_not_persist(self) -> None:
        with pytest.raises(PresetEditError):
            set_preset_entry("present", "review", "maybe")
        assert Mode.objects.by_name("present").entries == {"review": True}

    def test_unknown_preset_is_refused(self) -> None:
        with pytest.raises(PresetEditError):
            set_preset_entry("nope", "review", "on")

    def test_unknown_loop_is_refused(self) -> None:
        with pytest.raises(PresetEditError):
            set_preset_entry("present", "ghost", "on")


class PresetEntryResolverReflectionTestCase(TestCase):
    """An edit through the seam is immediately visible to the resolver ``preset show`` reads."""

    def setUp(self) -> None:
        _loop("review", enabled=False)
        _preset("present", {})
        ModeOverride.objects.set_override("present")
        self.addCleanup(ModeOverride.objects.clear)

    def _verdict(self, name: str) -> object:
        return next(verdict for verdict in effective_verdicts() if verdict.name == name)

    def test_absent_entry_falls_through_to_the_base_column(self) -> None:
        verdict = self._verdict("review")
        assert verdict.layer == "base"
        assert verdict.admitted is False

    def test_forcing_on_flips_the_verdict_and_the_deciding_layer(self) -> None:
        set_preset_entry("present", "review", "on")
        verdict = self._verdict("review")
        assert verdict.admitted is True
        assert verdict.layer == "override"

    def test_returning_to_inherit_restores_the_base_layer(self) -> None:
        set_preset_entry("present", "review", "on")
        set_preset_entry("present", "review", "inherit")
        verdict = self._verdict("review")
        assert verdict.admitted is False
        assert verdict.layer == "base"


class PresetActivationTestCase(TestCase):
    def setUp(self) -> None:
        _preset("maintenance", {})
        self.addCleanup(ModeOverride.objects.clear)

    def test_activate_sets_the_override_row(self) -> None:
        activate_preset("maintenance", hold=True)
        assert ModeOverride.objects.current().preset_name == "maintenance"

    def test_activate_with_hold_leaves_no_expiry(self) -> None:
        activate_preset("maintenance", hold=True)
        assert ModeOverride.objects.current().until is None

    def test_activate_with_ttl_sets_an_expiry(self) -> None:
        activate_preset("maintenance", until=timezone.now() + dt.timedelta(hours=2))
        assert ModeOverride.objects.current().until is not None

    def test_activate_unknown_preset_is_refused(self) -> None:
        with pytest.raises(PresetEditError):
            activate_preset("ghost", hold=True)
        assert ModeOverride.objects.current() is None

    def test_clear_removes_the_override(self) -> None:
        activate_preset("maintenance", hold=True)
        assert clear_preset_override() is True
        assert ModeOverride.objects.current() is None


class ActiveScheduleTestCase(TestCase):
    def setUp(self) -> None:
        _schedule("standard")
        self.addCleanup(ConfigSetting.objects.clear, ACTIVE_SCHEDULE_SETTING)

    def test_set_active_writes_the_config_setting(self) -> None:
        set_active_schedule("standard")
        assert active_schedule_name() == "standard"

    def test_set_active_unknown_schedule_is_refused(self) -> None:
        with pytest.raises(PresetEditError):
            set_active_schedule("holiday")
        assert active_schedule_name() == ""

    def test_clear_active_drops_the_setting(self) -> None:
        set_active_schedule("standard")
        assert clear_active_schedule() is True
        assert active_schedule_name() == ""


class ScheduleSlotEditingTestCase(TestCase):
    def setUp(self) -> None:
        self.schedule = _schedule("standard")
        _preset("present", {})

    def test_add_slot_persists_days_time_and_preset(self) -> None:
        slot = upsert_schedule_slot("standard", days=[0, 1, 2], start_time="08:30", preset_name="present")
        stored = ModeScheduleSlot.objects.get(pk=slot.pk)
        assert stored.weekdays == {0, 1, 2}
        assert stored.start_time.strftime("%H:%M") == "08:30"
        assert stored.preset_name == "present"

    def test_edit_slot_updates_in_place(self) -> None:
        slot = upsert_schedule_slot("standard", days=[0], start_time="08:00", preset_name="present")
        upsert_schedule_slot("standard", slot_id=slot.pk, days=[5, 6], start_time="20:00", preset_name="present")
        assert ModeScheduleSlot.objects.filter(schedule=self.schedule).count() == 1
        assert ModeScheduleSlot.objects.get(pk=slot.pk).weekdays == {5, 6}

    def test_slot_naming_an_unknown_preset_is_refused(self) -> None:
        with pytest.raises(PresetEditError):
            upsert_schedule_slot("standard", days=[0], start_time="08:00", preset_name="ghost")
        assert ModeScheduleSlot.objects.filter(schedule=self.schedule).count() == 0

    def test_slot_with_no_days_is_refused(self) -> None:
        with pytest.raises(PresetEditError):
            upsert_schedule_slot("standard", days=[], start_time="08:00", preset_name="present")

    def test_slot_with_a_bad_time_is_refused(self) -> None:
        with pytest.raises(PresetEditError):
            upsert_schedule_slot("standard", days=[0], start_time="25:99", preset_name="present")

    def test_delete_slot_removes_it(self) -> None:
        slot = upsert_schedule_slot("standard", days=[0], start_time="08:00", preset_name="present")
        delete_schedule_slot("standard", slot.pk)
        assert ModeScheduleSlot.objects.filter(schedule=self.schedule).count() == 0

    def test_delete_slot_from_the_wrong_schedule_is_refused(self) -> None:
        other = _schedule("holiday")
        slot = upsert_schedule_slot("standard", days=[0], start_time="08:00", preset_name="present")
        with pytest.raises(PresetEditError):
            delete_schedule_slot(other.name, slot.pk)
        assert ModeScheduleSlot.objects.filter(schedule=self.schedule).count() == 1


class LoadBearingRefusalTestCase(TestCase):
    """No mask may quiet the survival tier — the low-power mode alone excepted (#4188)."""

    def setUp(self) -> None:
        for name in LOAD_BEARING_LOOPS:
            _loop(name)
        _loop("review")
        self.preset = _preset("off", {})

    def test_masking_a_load_bearing_loop_off_is_refused_and_does_not_persist(self) -> None:
        with pytest.raises(PresetEditError) as exc:
            set_preset_entry("off", "resource_pressure", "off")

        assert "resource_pressure" in str(exc.value)
        assert Mode.objects.get(name="off").entries == {}

    def test_the_refusal_names_the_loop_and_the_one_mode_that_may(self) -> None:
        with pytest.raises(PresetEditError) as exc:
            set_preset_entry("off", "idle_stack_reaper", "off")

        message = str(exc.value)
        assert "idle_stack_reaper" in message
        assert "low-token" in message

    def test_the_low_power_mode_may_quiet_the_tier(self) -> None:
        _preset("low-token", {})

        set_preset_entry("low-token", "resource_pressure", "off")

        assert Mode.objects.get(name="low-token").entries == {"resource_pressure": False}

    def test_the_exception_follows_the_setting_not_the_shipped_name(self) -> None:
        _preset("token-guard", {})
        ConfigSetting.objects.set_value(LOW_POWER_PRESET_SETTING, "token-guard")
        _preset("low-token", {})

        set_preset_entry("token-guard", "resource_pressure", "off")
        with pytest.raises(PresetEditError):
            set_preset_entry("low-token", "resource_pressure", "off")

        assert Mode.objects.get(name="token-guard").entries == {"resource_pressure": False}

    def test_forcing_a_load_bearing_loop_on_is_allowed(self) -> None:
        set_preset_entry("off", "resource_pressure", "on")

        assert Mode.objects.get(name="off").entries == {"resource_pressure": True}

    def test_returning_a_load_bearing_loop_to_inherit_is_allowed(self) -> None:
        """Inherit hands the decision to the loop's own column — the mask quiets nothing."""
        _preset("off", {"resource_pressure": True})

        set_preset_entry("off", "resource_pressure", "inherit")

        assert Mode.objects.get(name="off").entries == {}

    def test_masking_a_work_loop_off_is_untouched(self) -> None:
        set_preset_entry("off", "review", "off")

        assert Mode.objects.get(name="off").entries == {"review": False}

    def test_an_already_quieted_row_is_refused_on_the_next_unrelated_edit(self) -> None:
        """A row written before the guard cannot be extended — the whole mask is judged."""
        _preset("off", {"resource_pressure": False})

        with pytest.raises(PresetEditError) as exc:
            set_preset_entry("off", "review", "off")

        assert "resource_pressure" in str(exc.value)


class BackupWithoutReclaimRefusalTestCase(TestCase):
    """No preset may admit ``db_backup`` once every reclaim loop is quiet (#4188).

    Reached through the BASE columns rather than the mask: the reclaim pair is off in
    ``Loop.enabled`` (never mentioned by the preset), so the load-bearing refusal above
    never fires — this is the shape it does not cover.
    """

    def setUp(self) -> None:
        _loop("db_backup")
        _loop("resource_pressure", enabled=False)
        _loop("idle_stack_reaper", enabled=False)
        _loop("review")
        self.preset = _preset("halt", {})

    def test_admitting_the_backup_over_a_base_disabled_reclaim_pair_is_refused(self) -> None:
        with pytest.raises(PresetEditError) as exc:
            set_preset_entry("halt", "db_backup", "on")

        message = str(exc.value)
        assert "db_backup" in message
        assert "resource_pressure" in message
        assert "idle_stack_reaper" in message
        assert Mode.objects.get(name="halt").entries == {}

    def test_an_already_admitted_row_is_refused_on_the_next_unrelated_edit(self) -> None:
        """A row written before this guard cannot be extended — the whole mask is judged."""
        _preset("halt", {"db_backup": True})

        with pytest.raises(PresetEditError) as exc:
            set_preset_entry("halt", "review", "off")

        assert "db_backup" in str(exc.value)

    def test_one_surviving_reclaim_loop_admits_the_backup(self) -> None:
        _loop("resource_pressure", enabled=True)

        set_preset_entry("halt", "db_backup", "on")

        assert Mode.objects.get(name="halt").entries == {"db_backup": True}

    def test_masking_the_backup_off_is_untouched(self) -> None:
        set_preset_entry("halt", "db_backup", "off")

        assert Mode.objects.get(name="halt").entries == {"db_backup": False}

    def test_the_low_power_escape_is_not_exempt_from_this_shape(self) -> None:
        """Low-power escapes the load-bearing refusal only — it never masks the reclaim pair off."""
        _preset("low-token", {})

        with pytest.raises(PresetEditError) as exc:
            set_preset_entry("low-token", "db_backup", "on")

        assert "db_backup" in str(exc.value)
