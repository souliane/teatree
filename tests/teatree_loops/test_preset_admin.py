"""Preset create/rename/delete/metadata — referrers move atomically or the write is refused (#3559)."""

import pytest
from django.test import TestCase

from teatree.core.mode_resolution import DEFAULT_MODE_SETTING, mode_name_for_availability, resolve_active_mode
from teatree.core.models import ConfigSetting, Mode, ModeOverride, ModeSchedule, ModeScheduleSlot
from teatree.core.models.loop_preset import LOW_POWER_PRESET_SETTING
from teatree.loops.preset_admin import create_preset, delete_preset, preset_referrers, rename_preset, update_preset_meta
from teatree.loops.preset_editing import PresetEditError


def _preset(name: str, **fields: object) -> Mode:
    preset, _ = Mode.objects.update_or_create(name=name, defaults={"entries": {}, **fields})
    return preset


class CreatePresetTestCase(TestCase):
    def test_create_persists_name_and_description(self) -> None:
        create_preset("night-shift", description="Nights only.")
        assert Mode.objects.by_name("night-shift").description == "Nights only."

    def test_create_starts_with_no_opinion_on_anything(self) -> None:
        create_preset("night-shift")
        assert Mode.objects.by_name("night-shift").entries == {}

    def test_duplicate_name_is_refused(self) -> None:
        create_preset("night-shift")
        with pytest.raises(PresetEditError):
            create_preset("night-shift")

    def test_non_slug_name_is_refused(self) -> None:
        with pytest.raises(PresetEditError):
            create_preset("night shift!")


class PresetMetadataTestCase(TestCase):
    def setUp(self) -> None:
        _preset("low-power", description="stale text", availability_mode="away")

    def test_description_is_editable(self) -> None:
        update_preset_meta("low-power", description="Token-budget guard.")
        assert Mode.objects.by_name("low-power").description == "Token-budget guard."

    def test_pin_is_switchable(self) -> None:
        update_preset_meta("low-power", availability_pin="autonomous_away")
        assert Mode.objects.by_name("low-power").availability_pin == "autonomous_away"

    def test_pin_is_clearable(self) -> None:
        update_preset_meta("low-power", availability_pin="")
        assert Mode.objects.by_name("low-power").availability_pin is None

    def test_unknown_pin_is_refused(self) -> None:
        with pytest.raises(PresetEditError):
            update_preset_meta("low-power", availability_pin="vacation")
        assert Mode.objects.by_name("low-power").availability_pin == "away"


class RenamePresetTestCase(TestCase):
    """A rename re-points every by-name referrer in one transaction — never orphans one."""

    def setUp(self) -> None:
        _preset("low-power", description="Token-budget guard.")
        _preset("engaged")
        self.schedule = ModeSchedule.objects.create(name="renametest")
        self.addCleanup(ConfigSetting.objects.clear, LOW_POWER_PRESET_SETTING)
        self.addCleanup(ModeOverride.objects.clear)

    def test_rename_moves_the_row(self) -> None:
        rename_preset("low-power", "low-tokens")
        assert Mode.objects.by_name("low-power") is None
        assert Mode.objects.by_name("low-tokens").description == "Token-budget guard."

    def test_rename_repoints_the_auto_engage_setting(self) -> None:
        ConfigSetting.objects.set_value(LOW_POWER_PRESET_SETTING, "low-power")
        rename_preset("low-power", "low-tokens")
        assert ConfigSetting.objects.get_effective(LOW_POWER_PRESET_SETTING) == "low-tokens"

    def test_rename_repoints_a_setting_left_at_its_default(self) -> None:
        # The auto-engage target defaults to "low-power" with no row written; a rename
        # must pin the new name so the unset default cannot dangle.
        rename_preset("low-power", "low-tokens")
        assert ConfigSetting.objects.get_effective(LOW_POWER_PRESET_SETTING) == "low-tokens"

    def test_rename_repoints_schedule_slots(self) -> None:
        slot = ModeScheduleSlot.objects.create(
            schedule=self.schedule, days=[0], start_time="08:00", preset_name="low-power"
        )
        rename_preset("low-power", "low-tokens")
        assert ModeScheduleSlot.objects.get(pk=slot.pk).preset_name == "low-tokens"

    def test_rename_repoints_the_live_override(self) -> None:
        ModeOverride.objects.set_override("low-power")
        rename_preset("low-power", "low-tokens")
        assert ModeOverride.objects.current().preset_name == "low-tokens"
        assert resolve_active_mode().name == "low-tokens"

    def test_rename_repoints_the_default_mode_setting(self) -> None:
        ConfigSetting.objects.set_value(DEFAULT_MODE_SETTING, "engaged")
        self.addCleanup(ConfigSetting.objects.clear, DEFAULT_MODE_SETTING)
        rename_preset("engaged", "working")
        assert ConfigSetting.objects.get_effective(DEFAULT_MODE_SETTING) == "working"

    def test_rename_onto_an_existing_name_is_refused(self) -> None:
        with pytest.raises(PresetEditError):
            rename_preset("low-power", "engaged")
        assert Mode.objects.by_name("low-power") is not None

    def test_rename_to_a_non_slug_is_refused(self) -> None:
        with pytest.raises(PresetEditError):
            rename_preset("low-power", "low tokens!")
        assert Mode.objects.by_name("low-power") is not None

    def test_rename_of_an_unknown_preset_is_refused(self) -> None:
        with pytest.raises(PresetEditError):
            rename_preset("ghost", "spectre")


class DeletePresetTestCase(TestCase):
    def setUp(self) -> None:
        _preset("spare")
        _preset("engaged")
        self.schedule = ModeSchedule.objects.create(name="deletetest")
        self.addCleanup(ModeOverride.objects.clear)
        self.addCleanup(ConfigSetting.objects.clear, LOW_POWER_PRESET_SETTING)

    def test_unreferenced_preset_is_deleted(self) -> None:
        delete_preset("spare")
        assert Mode.objects.by_name("spare") is None

    def test_active_preset_is_refused(self) -> None:
        ModeOverride.objects.set_override("spare")
        with pytest.raises(PresetEditError, match="active"):
            delete_preset("spare")
        assert Mode.objects.by_name("spare") is not None

    def test_preset_used_by_a_schedule_slot_is_refused(self) -> None:
        ModeScheduleSlot.objects.create(schedule=self.schedule, days=[0], start_time="08:00", preset_name="spare")
        with pytest.raises(PresetEditError, match="deletetest"):
            delete_preset("spare")
        assert Mode.objects.by_name("spare") is not None

    def test_preset_named_by_a_setting_is_refused(self) -> None:
        ConfigSetting.objects.set_value(LOW_POWER_PRESET_SETTING, "spare")
        with pytest.raises(PresetEditError, match=LOW_POWER_PRESET_SETTING):
            delete_preset("spare")
        assert Mode.objects.by_name("spare") is not None

    def test_referrers_are_reported_for_the_ui(self) -> None:
        ModeScheduleSlot.objects.create(schedule=self.schedule, days=[0], start_time="08:00", preset_name="spare")
        referrers = preset_referrers("spare")
        assert referrers.schedule_slots
        assert referrers.blocks_delete is True

    def test_unknown_preset_delete_is_refused(self) -> None:
        with pytest.raises(PresetEditError):
            delete_preset("ghost")


class AvailabilityResolvesByRowTestCase(TestCase):
    """A mode is selected by its intrinsic posture, so renaming it cannot break behaviour."""

    def setUp(self) -> None:
        _preset("engaged", defers_questions=False, pauses_self_pump=False)
        _preset("unattended", defers_questions=True, pauses_self_pump=False)
        _preset("offline", defers_questions=True, pauses_self_pump=True)

    def test_present_resolves_to_the_reachable_mode(self) -> None:
        assert mode_name_for_availability("present") == "engaged"

    def test_autonomous_away_resolves_to_the_defer_but_keep_pumping_mode(self) -> None:
        assert mode_name_for_availability("autonomous_away") == "unattended"

    def test_away_resolves_to_the_holiday_mode(self) -> None:
        assert mode_name_for_availability("away") == "offline"

    def test_a_renamed_mode_still_resolves(self) -> None:
        rename_preset("offline", "holiday")
        assert mode_name_for_availability("away") == "holiday"

    def test_an_unknown_token_is_refused(self) -> None:
        with pytest.raises(LookupError):
            mode_name_for_availability("vacation")
