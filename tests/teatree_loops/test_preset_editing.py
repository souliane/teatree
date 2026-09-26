"""The shared preset/schedule write seam — tri-state round-trip incl. genuine absence (#3559)."""

import datetime as dt
from unittest.mock import patch

import pytest
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import ConfigSetting, Loop, Mode, ModeOverride, ModeSchedule, ModeScheduleSlot
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING
from teatree.loops import preset_editing
from teatree.loops.enable_verdict import effective_verdicts
from teatree.loops.preset_editing import PresetEditError, activate_preset, clear_preset_override, set_preset_entry
from teatree.loops.schedule_editing import (
    active_schedule_name,
    clear_active_schedule,
    delete_schedule_slot,
    set_active_schedule,
    upsert_schedule_slot,
)


def _loop(name: str) -> Loop:
    """The seeded row for *name*, carrying no manual override (the default loops ship seeded)."""
    loop, _ = Loop.objects.update_or_create(
        name=name,
        defaults={"script": f"src/teatree/loops/{name}/loop.py", "delay_seconds": 60, "enabled": None},
    )
    return loop


def _preset(name: str, entries: dict[str, bool]) -> Mode:
    preset, _ = Mode.objects.update_or_create(name=name, defaults={"entries": entries})
    return preset


def _schedule(name: str) -> ModeSchedule:
    schedule, _ = ModeSchedule.objects.get_or_create(name=name)
    ModeScheduleSlot.objects.filter(schedule=schedule).delete()
    return schedule


class PresetEntryTotalityTestCase(TestCase):
    """Every write leaves the map answering for every live loop — no absent tier (B1)."""

    def setUp(self) -> None:
        _loop("review")
        self.preset = _preset("present", {"review": True})

    def test_setting_off_stores_false(self) -> None:
        set_preset_entry("present", "review", "off")
        assert Mode.objects.by_name("present").state_for("review") is False

    def test_setting_on_stores_true(self) -> None:
        set_preset_entry("present", "review", "off")
        set_preset_entry("present", "review", "on")
        assert Mode.objects.by_name("present").state_for("review") is True

    def test_a_partial_map_is_repaired_by_the_next_write(self) -> None:
        set_preset_entry("present", "review", "on")

        entries = Mode.objects.by_name("present").entries
        assert set(entries) == set(Loop.objects.values_list("name", flat=True))
        assert all(isinstance(value, bool) for value in entries.values())

    def test_a_loop_the_map_never_named_lands_off_rather_than_inheriting(self) -> None:
        _loop("dream")

        set_preset_entry("present", "review", "on")

        assert Mode.objects.by_name("present").state_for("dream") is False

    def test_inherit_is_no_longer_a_value(self) -> None:
        with pytest.raises(PresetEditError):
            set_preset_entry("present", "review", "inherit")

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

    def test_the_row_is_read_inside_the_write_transaction(self) -> None:
        # ``entries`` is ONE JSON map, so a read taken BEFORE the write transaction drops
        # whatever the other editor (dashboard vs CLI) stored in between. SQLite opens in
        # IMMEDIATE mode, so reading inside the block makes the edit a real
        # compare-and-swap — a property of WHERE the read happens, and nothing else.
        depth_outside = len(connection.savepoint_ids)
        depth_at_read: list[int] = []
        real_require = preset_editing.require_preset

        def _recording_require(name: str) -> Mode:
            depth_at_read.append(len(connection.savepoint_ids))
            return real_require(name)

        with patch.object(preset_editing, "require_preset", _recording_require):
            set_preset_entry("present", "review", "off")

        assert depth_at_read == [depth_outside + 1]
        assert Mode.objects.by_name("present").state_for("review") is False


class PresetEntryResolverReflectionTestCase(TestCase):
    """An edit through the seam is immediately visible to the resolver ``preset show`` reads."""

    def setUp(self) -> None:
        _loop("review")
        _preset("present", {"review": False})
        ModeOverride.objects.set_override("present", reason="test override")
        self.addCleanup(ModeOverride.objects.clear)

    def _verdict(self, name: str) -> object:
        return next(verdict for verdict in effective_verdicts() if verdict.name == name)

    def test_the_preset_decides_and_names_itself(self) -> None:
        verdict = self._verdict("review")
        assert verdict.layer == "override"
        assert verdict.admitted is False

    def test_forcing_on_flips_the_verdict_and_the_deciding_layer(self) -> None:
        set_preset_entry("present", "review", "on")
        verdict = self._verdict("review")
        assert verdict.admitted is True
        assert verdict.layer == "override"

    def test_masking_off_again_flips_it_back(self) -> None:
        set_preset_entry("present", "review", "on")
        set_preset_entry("present", "review", "off")
        verdict = self._verdict("review")
        assert verdict.admitted is False
        assert verdict.layer == "override"


class PresetActivationTestCase(TestCase):
    def setUp(self) -> None:
        _preset("maintenance", {})
        self.addCleanup(ModeOverride.objects.clear)

    def test_activate_sets_the_override_row(self) -> None:
        activate_preset("maintenance", reason="test override")
        assert ModeOverride.objects.current().preset_name == "maintenance"

    def test_activate_holds_until_someone_clears_it(self) -> None:
        activate_preset("maintenance", reason="test override")
        assert ModeOverride.objects.current().expected_lift_at is None

    def test_activate_records_the_advisory_lift_by(self) -> None:
        activate_preset("maintenance", reason="test override", expected_lift_at=timezone.now() + dt.timedelta(hours=2))
        assert ModeOverride.objects.current().expected_lift_at is not None

    def test_activate_unknown_preset_is_refused(self) -> None:
        with pytest.raises(PresetEditError):
            activate_preset("ghost", reason="test override")
        assert ModeOverride.objects.current() is None

    def test_clear_removes_the_override(self) -> None:
        activate_preset("maintenance", reason="test override")
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


class BackupWithoutReclaimRefusalTestCase(TestCase):
    """No preset may admit ``db_backup`` once every reclaim loop is quiet — the one rule left."""

    def setUp(self) -> None:
        _loop("db_backup")
        _loop("resource_pressure")
        _loop("idle_stack_reaper")
        _loop("review")
        self.preset = _preset("halt", {"resource_pressure": False, "idle_stack_reaper": False})

    def test_admitting_the_backup_over_a_quiet_reclaim_pair_is_refused(self) -> None:
        with pytest.raises(PresetEditError) as exc:
            set_preset_entry("halt", "db_backup", "on")

        message = str(exc.value)
        assert "db_backup" in message
        assert "resource_pressure" in message
        assert "idle_stack_reaper" in message
        assert Mode.objects.get(name="halt").state_for("db_backup") is False

    def test_an_already_admitted_row_is_refused_on_the_next_unrelated_edit(self) -> None:
        """A row written before this guard cannot be extended — the whole mask is judged."""
        _preset("halt", {"db_backup": True, "resource_pressure": False, "idle_stack_reaper": False})

        with pytest.raises(PresetEditError) as exc:
            set_preset_entry("halt", "review", "off")

        assert "db_backup" in str(exc.value)

    def test_one_surviving_reclaim_loop_admits_the_backup(self) -> None:
        set_preset_entry("halt", "resource_pressure", "on")

        set_preset_entry("halt", "db_backup", "on")

        assert Mode.objects.get(name="halt").state_for("db_backup") is True

    def test_masking_the_backup_off_is_untouched(self) -> None:
        set_preset_entry("halt", "db_backup", "off")

        assert Mode.objects.get(name="halt").state_for("db_backup") is False

    def test_the_token_outage_preset_is_not_exempt_from_this_shape(self) -> None:
        _preset("token-outage", {"resource_pressure": False, "idle_stack_reaper": False})

        with pytest.raises(PresetEditError) as exc:
            set_preset_entry("token-outage", "db_backup", "on")

        assert "db_backup" in str(exc.value)
