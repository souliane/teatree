"""Idempotent seed of the default presets + schedules (#3159).

``t3 setup`` seeds the 6 curated presets and the ``standard`` /
``always-unattended`` schedules as owner-editable DB data — fully opt-in
(``active_loop_schedule`` stays unset). Integration-first against the real DB.
"""

import io

import django.test
from django.core.management import call_command

from teatree.core.models import ConfigSetting, Mode, ModeSchedule, ModeScheduleSlot
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING
from teatree.loops.preset_seed import seed_default_presets_and_schedules
from teatree.loops.seed import DEFAULT_LOOPS

_EXPECTED_PRESETS = {"engaged", "heads-down", "unattended", "maintenance", "low-power", "off", "offline"}
_EXPECTED_SCHEDULES = {"standard", "always-unattended"}


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestSeedDefaultPresets(django.test.TestCase):
    def setUp(self) -> None:
        Mode.objects.all().delete()
        ModeSchedule.objects.all().delete()

    def test_seeds_the_six_presets_and_two_schedules(self) -> None:
        result = seed_default_presets_and_schedules()
        assert result.presets_created == len(_EXPECTED_PRESETS)
        assert result.schedules_created == len(_EXPECTED_SCHEDULES)
        assert set(Mode.objects.values_list("name", flat=True)) == _EXPECTED_PRESETS
        assert set(ModeSchedule.objects.values_list("name", flat=True)) == _EXPECTED_SCHEDULES

    def test_off_forces_every_seeded_loop_off(self) -> None:
        seed_default_presets_and_schedules()
        entries = Mode.objects.get(name="off").entries
        assert all(value is False for value in entries.values())
        assert set(entries) == {spec.name for spec in DEFAULT_LOOPS}

    def test_low_power_keeps_only_deterministic_local_loops(self) -> None:
        seed_default_presets_and_schedules()
        entries = Mode.objects.get(name="low-power").entries
        assert entries["inbox"] is True
        assert entries["housekeeping"] is True
        assert entries["review"] is False
        assert entries["dispatch"] is False

    def test_unattended_pins_autonomous_away(self) -> None:
        seed_default_presets_and_schedules()
        assert Mode.objects.get(name="unattended").availability_pin == "autonomous_away"

    def test_mode_booleans_seeded_per_recommended_table(self) -> None:
        seed_default_presets_and_schedules()
        # present-class: never defers.
        for name in ("engaged", "heads-down", "off"):
            preset = Mode.objects.get(name=name)
            assert preset.defers_questions is False
            assert preset.pauses_self_pump is False
        # away-class autonomous: defers, keeps pumping.
        for name in ("unattended", "maintenance", "low-power"):
            preset = Mode.objects.get(name=name)
            assert preset.defers_questions is True
            assert preset.pauses_self_pump is False

    def test_offline_is_the_holiday_away_mode(self) -> None:
        seed_default_presets_and_schedules()
        offline = Mode.objects.get(name="offline")
        assert offline.defers_questions is True
        assert offline.pauses_self_pump is True
        assert offline.presence_sensitive is False
        assert all(value is False for value in offline.entries.values())

    def test_destructive_loops_inherit_in_engaged(self) -> None:
        seed_default_presets_and_schedules()
        entries = Mode.objects.get(name="engaged").entries
        for name in ("issue_implementer", "backlog_sweep", "outer_loop", "directive_loop"):
            assert name not in entries

    def test_standard_schedule_has_the_expected_slots(self) -> None:
        seed_default_presets_and_schedules()
        standard = ModeSchedule.objects.get(name="standard")
        presets = sorted(standard.slots.values_list("preset_name", flat=True))
        assert presets == ["engaged", "maintenance", "unattended", "unattended"]

    def test_active_schedule_is_unset_after_seed(self) -> None:
        seed_default_presets_and_schedules()
        assert ConfigSetting.objects.get_effective(ACTIVE_SCHEDULE_SETTING) is None

    def test_idempotent_second_run_creates_nothing(self) -> None:
        seed_default_presets_and_schedules()
        again = seed_default_presets_and_schedules()
        assert again.presets_created == 0
        assert again.schedules_created == 0
        assert ModeScheduleSlot.objects.filter(schedule__name="standard").count() == 4

    def test_seed_never_clobbers_an_edited_preset(self) -> None:
        seed_default_presets_and_schedules()
        preset = Mode.objects.get(name="off")
        preset.entries = {"inbox": True}
        preset.save()
        seed_default_presets_and_schedules()
        assert Mode.objects.get(name="off").entries == {"inbox": True}

    def test_management_command_reports_creates(self) -> None:
        Mode.objects.all().delete()
        ModeSchedule.objects.all().delete()
        out = io.StringIO()
        call_command("seed_loops", stdout=out)
        assert "presets:" in out.getvalue()
