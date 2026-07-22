"""Idempotent seed of the default presets + schedules (#3159).

``t3 setup`` seeds the 7 curated presets and the ``standard`` /
``always-unattended`` schedules as owner-editable DB data. ``standard`` ships as
the active schedule (owner working hours, Europe/Vienna), pinned through the
provenance-aware ``ConfigSetting.seed`` so an operator switch is never clobbered.
Integration-first against the real DB.
"""

import datetime as dt
import io
import zoneinfo

import django.test
from django.core.management import call_command

from teatree.core.models import ConfigSetting, Mode, ModeSchedule, ModeScheduleSlot
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING, resolve_active_preset
from teatree.loops.preset_seed import default_preset_specs, seed_default_presets_and_schedules
from teatree.loops.seed import DEFAULT_LOOPS

_EXPECTED_PRESETS = {"engaged", "heads-down", "unattended", "maintenance", "low-power", "off", "offline"}
_EXPECTED_SCHEDULES = {"standard", "always-unattended"}
_VIENNA = zoneinfo.ZoneInfo("Europe/Vienna")


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestSeedDefaultPresets(django.test.TestCase):
    def setUp(self) -> None:
        Mode.objects.all().delete()
        ModeSchedule.objects.all().delete()

    def test_seeds_the_seven_presets_and_two_schedules(self) -> None:
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

    def test_standard_schedule_has_the_owner_working_hours_slots(self) -> None:
        seed_default_presets_and_schedules()
        standard = ModeSchedule.objects.get(name="standard")
        slots = {(tuple(sorted(slot.weekdays)), slot.start_time, slot.preset_name) for slot in standard.slots.all()}
        assert slots == {
            ((0, 1, 2, 3, 4), dt.time(9, 0), "engaged"),
            ((0, 1, 2, 3, 4), dt.time(16, 0), "unattended"),
            ((5, 6), dt.time(0, 0), "unattended"),
        }

    def test_standard_schedule_uses_the_vienna_timezone(self) -> None:
        seed_default_presets_and_schedules()
        assert ModeSchedule.objects.get(name="standard").timezone == "Europe/Vienna"

    def test_every_preset_entry_names_a_valid_loop(self) -> None:
        loop_names = {spec.name for spec in DEFAULT_LOOPS}
        for spec in default_preset_specs():
            unknown = set(spec.entries) - loop_names
            assert not unknown, f"preset {spec.name!r} names unknown loops: {sorted(unknown)}"

    def test_standard_ships_as_the_active_schedule(self) -> None:
        seed_default_presets_and_schedules()
        assert ConfigSetting.objects.get_effective(ACTIVE_SCHEDULE_SETTING) == "standard"

    def test_idempotent_second_run_creates_nothing(self) -> None:
        seed_default_presets_and_schedules()
        again = seed_default_presets_and_schedules()
        assert again.presets_created == 0
        assert again.schedules_created == 0
        assert ModeScheduleSlot.objects.filter(schedule__name="standard").count() == 3
        assert ConfigSetting.objects.get_effective(ACTIVE_SCHEDULE_SETTING) == "standard"

    def test_reseed_never_clobbers_an_operator_switched_active_schedule(self) -> None:
        seed_default_presets_and_schedules()
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "always-unattended")
        seed_default_presets_and_schedules()
        assert ConfigSetting.objects.get_effective(ACTIVE_SCHEDULE_SETTING) == "always-unattended"

    def test_seed_never_clobbers_an_edited_preset(self) -> None:
        seed_default_presets_and_schedules()
        preset = Mode.objects.get(name="off")
        preset.entries = {"inbox": True}
        preset.save()
        seed_default_presets_and_schedules()
        assert Mode.objects.get(name="off").entries == {"inbox": True}

    def test_reseed_never_clobbers_an_operator_rearranged_schedule(self) -> None:
        seed_default_presets_and_schedules()
        ModeScheduleSlot.objects.filter(schedule__name="standard").delete()
        seed_default_presets_and_schedules()
        assert ModeScheduleSlot.objects.filter(schedule__name="standard").count() == 0


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestSeededStandardScheduleResolvesViennaHours(django.test.TestCase):
    """The seeded ``standard`` calendar resolves the owner's Europe/Vienna working hours.

    Attended (``engaged``) Mon-Fri 09:00-16:00; every other hour is ``unattended``.
    Slots are wall-clock in Europe/Vienna, so the same Sat 12:00 resolves ``unattended``
    across the DST boundary (summer CEST UTC+2 and winter CET UTC+1) with no hardcoded
    offset.
    """

    def setUp(self) -> None:
        Mode.objects.all().delete()
        ModeSchedule.objects.all().delete()
        seed_default_presets_and_schedules()

    def _active_at(self, moment: dt.datetime) -> str | None:
        active = resolve_active_preset(now=moment)
        return active.preset.name if active is not None else None

    def test_weekday_working_hours_resolve_engaged(self) -> None:
        # 2026-07-14 is a Tuesday (summer, CEST UTC+2).
        assert self._active_at(dt.datetime(2026, 7, 14, 10, 0, tzinfo=_VIENNA)) == "engaged"

    def test_weekday_evening_resolves_unattended(self) -> None:
        assert self._active_at(dt.datetime(2026, 7, 14, 22, 0, tzinfo=_VIENNA)) == "unattended"

    def test_weekday_early_morning_resolves_unattended(self) -> None:
        assert self._active_at(dt.datetime(2026, 7, 14, 7, 0, tzinfo=_VIENNA)) == "unattended"

    def test_summer_saturday_resolves_unattended(self) -> None:
        # 2026-07-18 is a Saturday under CEST (UTC+2).
        assert self._active_at(dt.datetime(2026, 7, 18, 12, 0, tzinfo=_VIENNA)) == "unattended"

    def test_winter_saturday_resolves_unattended_across_the_dst_boundary(self) -> None:
        # 2026-01-17 is a Saturday under CET (UTC+1) — the DST counterpart of the summer case.
        assert self._active_at(dt.datetime(2026, 1, 17, 12, 0, tzinfo=_VIENNA)) == "unattended"

    def test_management_command_reports_creates(self) -> None:
        Mode.objects.all().delete()
        ModeSchedule.objects.all().delete()
        out = io.StringIO()
        call_command("seed_loops", stdout=out)
        assert "presets:" in out.getvalue()
