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
from pathlib import Path

import django.test
from django.core.management import call_command

from teatree.config.seed_defaults import shipped_seed_table
from teatree.core.models import ConfigSetting, Mode, ModeSchedule, ModeScheduleSlot
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING, resolve_active_preset
from teatree.loops.preset_seed import (
    PresetSpec,
    ScheduleSpec,
    SlotSpec,
    default_preset_specs,
    default_schedule_specs,
    seed_default_presets_and_schedules,
)
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


#: The shipped mask of every mode, as (forced ON, forced OFF) loop names — a loop in
#: NEITHER set is absent from the mode and inherits its own enabled flag. Transcribed from
#: the pre-move in-code constants and pinned here so relocating the data into
#: ``defaults.toml`` cannot retune a single loop.
_SHIPPED_MASKS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "engaged": (
        frozenset(
            {
                "inbox",
                "dispatch",
                "tickets",
                "ship",
                "review",
                "followup",
                "audit",
                "news",
                "arch_review",
                "dream",
                "eval_local",
                "dogfood",
                "snapshot_warmer",
                "housekeeping",
                "idle_stack_reaper",
                "local_stack_queue",
                "resource_pressure",
            }
        ),
        frozenset(),
    ),
    "heads-down": (
        frozenset(
            {
                "inbox",
                "dispatch",
                "tickets",
                "ship",
                "dream",
                "snapshot_warmer",
                "housekeeping",
                "idle_stack_reaper",
                "local_stack_queue",
                "resource_pressure",
            }
        ),
        frozenset({"review", "followup", "audit", "news", "arch_review", "eval_local", "dogfood"}),
    ),
    "unattended": (
        frozenset(
            {
                "inbox",
                "dispatch",
                "tickets",
                "ship",
                "review",
                "audit",
                "news",
                "arch_review",
                "dream",
                "snapshot_warmer",
                "housekeeping",
                "idle_stack_reaper",
                "local_stack_queue",
                "resource_pressure",
            }
        ),
        frozenset({"followup"}),
    ),
    "maintenance": (
        frozenset(
            {
                "inbox",
                "dispatch",
                "dream",
                "eval_local",
                "dogfood",
                "arch_review",
                "news",
                "snapshot_warmer",
                "housekeeping",
                "idle_stack_reaper",
                "local_stack_queue",
                "resource_pressure",
            }
        ),
        frozenset({"tickets", "ship", "review", "followup", "audit"}),
    ),
    "low-power": (
        frozenset({"inbox", "idle_stack_reaper", "local_stack_queue", "resource_pressure", "housekeeping"}),
        frozenset(spec.name for spec in DEFAULT_LOOPS)
        - frozenset({"inbox", "idle_stack_reaper", "local_stack_queue", "resource_pressure", "housekeeping"}),
    ),
    "off": (frozenset(), frozenset(spec.name for spec in DEFAULT_LOOPS)),
    "offline": (frozenset(), frozenset(spec.name for spec in DEFAULT_LOOPS)),
}

#: ``(availability_mode, defers_questions, pauses_self_pump, presence_sensitive)`` per mode.
_SHIPPED_POSTURES: dict[str, tuple[str, bool, bool, bool]] = {
    "engaged": ("", False, False, True),
    "heads-down": ("", False, False, True),
    "unattended": ("autonomous_away", True, False, True),
    "maintenance": ("", True, False, True),
    "low-power": ("", True, False, True),
    "off": ("", False, False, True),
    "offline": ("away", True, True, False),
}


class TestShippedSpecsAreUnchangedByTheMoveIntoTheFile:
    """The relocation into ``defaults.toml`` retunes nothing — every mask and posture holds."""

    def test_every_mode_ships_its_recorded_mask(self) -> None:
        by_name = {spec.name: spec.entries for spec in default_preset_specs()}
        assert set(by_name) == set(_SHIPPED_MASKS)
        for name, (on, off) in _SHIPPED_MASKS.items():
            entries = by_name[name]
            assert {loop for loop, value in entries.items() if value} == on, name
            assert {loop for loop, value in entries.items() if not value} == off, name

    def test_every_mode_ships_its_recorded_availability_posture(self) -> None:
        for spec in default_preset_specs():
            posture = (spec.availability_mode, spec.defers_questions, spec.pauses_self_pump, spec.presence_sensitive)
            assert posture == _SHIPPED_POSTURES[spec.name], spec.name

    def test_the_exhaustive_modes_name_every_shipped_loop(self) -> None:
        # `low-power` / `off` / `offline` used to be built programmatically over every seed
        # spec, so a new loop was covered automatically. As shipped DATA they must name each
        # loop explicitly — an omitted one would silently INHERIT instead of being masked off.
        shipped = {spec.name for spec in DEFAULT_LOOPS}
        for name in ("low-power", "off", "offline"):
            entries = next(spec.entries for spec in default_preset_specs() if spec.name == name)
            assert set(entries) == shipped, name

    def test_always_unattended_is_one_all_week_slot(self) -> None:
        holiday = next(spec for spec in default_schedule_specs() if spec.name == "always-unattended")
        assert holiday.timezone == ""
        assert [(slot.days, slot.start_time, slot.preset_name) for slot in holiday.slots] == [
            ([0, 1, 2, 3, 4, 5, 6], dt.time(0, 0), "unattended")
        ]


class TestSpecsAreShippedDataNotCode:
    """The mode / schedule specs are built from the shipped ``defaults.toml`` tables."""

    def test_mode_specs_are_loaded_from_the_file_they_are_pointed_at(self, tmp_path: Path) -> None:
        fixture = tmp_path / "defaults.toml"
        fixture.write_text(
            "[modes.sentinel]\n"
            'description = "a synthetic mode"\n'
            'availability_mode = "away"\n'
            "defers_questions = true\n"
            "pauses_self_pump = true\n"
            "presence_sensitive = false\n"
            "[modes.sentinel.entries]\n"
            "inbox = true\n"
            "dispatch = false\n",
            encoding="utf-8",
        )
        (spec,) = default_preset_specs(fixture)
        assert spec == PresetSpec(
            name="sentinel",
            description="a synthetic mode",
            entries={"inbox": True, "dispatch": False},
            availability_mode="away",
            defers_questions=True,
            pauses_self_pump=True,
            presence_sensitive=False,
        )

    def test_schedule_specs_are_loaded_from_the_file_they_are_pointed_at(self, tmp_path: Path) -> None:
        fixture = tmp_path / "defaults.toml"
        fixture.write_text(
            "[schedules.sentinel]\n"
            'description = "a synthetic calendar"\n'
            'timezone = "UTC"\n'
            "[[schedules.sentinel.slots]]\n"
            "days = [2, 3]\n"
            "start_time = 07:15:00\n"
            'preset_name = "engaged"\n',
            encoding="utf-8",
        )
        (spec,) = default_schedule_specs(fixture)
        assert spec == ScheduleSpec(
            name="sentinel",
            description="a synthetic calendar",
            slots=(SlotSpec(days=[2, 3], start_time=dt.time(7, 15), preset_name="engaged"),),
            timezone="UTC",
        )

    def test_an_omitted_optional_field_falls_back_to_the_dataclass_default(self, tmp_path: Path) -> None:
        fixture = tmp_path / "defaults.toml"
        fixture.write_text(
            '[modes.sentinel]\ndescription = "x"\n[schedules.cal]\ndescription = "y"\n', encoding="utf-8"
        )
        (mode,) = default_preset_specs(fixture)
        (schedule,) = default_schedule_specs(fixture)
        assert (mode.entries, mode.availability_mode, mode.defers_questions) == ({}, "", False)
        assert (mode.pauses_self_pump, mode.presence_sensitive) == (False, True)
        assert (schedule.slots, schedule.timezone) == ((), "")

    def test_every_shipped_mode_and_schedule_name_matches_the_file(self) -> None:
        assert {spec.name for spec in default_preset_specs()} == set(shipped_seed_table("modes"))
        assert {spec.name for spec in default_schedule_specs()} == set(shipped_seed_table("schedules"))
