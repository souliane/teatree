"""Idempotent seed of the default presets + schedules (#3159, #4202).

``t3 setup`` seeds the 5 curated presets and the ``standard`` / ``always-away``
schedules as owner-editable DB data. ``standard`` ships as the active schedule (owner
working hours, Europe/Vienna), pinned through the provenance-aware
``ConfigSetting.seed`` so an operator switch is never clobbered. Integration-first
against the real DB.
"""

import datetime as dt
import io
import zoneinfo
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

import django.test
import pytest
from django.core.management import call_command
from django.db.utils import OperationalError

from teatree.config.seed_defaults import shipped_seed_table
from teatree.core.mode_resolution import resolve_active_mode
from teatree.core.models import ConfigSetting, Mode, ModeOverride, ModeSchedule, ModeScheduleSlot
from teatree.core.models.loop_preset import DEFAULT_LOW_POWER_PRESET
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING, resolve_active_preset
from teatree.loops.mode_shape import (
    INTAKE_LOOPS,
    LOAD_BEARING_LOOPS,
    backup_without_reclaim,
    intake_without_delivery,
    quieted_load_bearing,
)
from teatree.loops.preset_seed import (
    PresetSpec,
    ScheduleSpec,
    SlotSpec,
    default_preset_specs,
    default_schedule_specs,
    seed_default_presets_and_schedules,
)
from teatree.loops.seed import DEFAULT_LOOPS, load_loop_specs

_EXPECTED_PRESETS = {"present", "away", "maintenance", "low-token", "off"}
_EXPECTED_SCHEDULES = {"standard", "always-away"}
#: The pre-#4202 names. Seeding one again would resurrect a preset the collapse retired.
_RETIRED_PRESETS = {"engaged", "heads-down", "low-power", "unattended", "offline"}
_VIENNA = zoneinfo.ZoneInfo("Europe/Vienna")


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestSeedDefaultPresets(django.test.TestCase):
    def setUp(self) -> None:
        ModeOverride.objects.all().delete()
        Mode.objects.all().delete()
        ModeSchedule.objects.all().delete()

    def test_seeds_the_five_presets_and_two_schedules(self) -> None:
        result = seed_default_presets_and_schedules()
        assert result.presets_created == len(_EXPECTED_PRESETS)
        assert result.schedules_created == len(_EXPECTED_SCHEDULES)
        assert set(Mode.objects.values_list("name", flat=True)) == _EXPECTED_PRESETS
        assert set(ModeSchedule.objects.values_list("name", flat=True)) == _EXPECTED_SCHEDULES

    def test_no_retired_preset_name_is_seeded(self) -> None:
        seed_default_presets_and_schedules()
        assert not Mode.objects.filter(name__in=_RETIRED_PRESETS).exists()

    def test_off_forces_every_work_loop_off_and_the_load_bearing_tier_on(self) -> None:
        """A halt mode stops the WORK, never the tier that can still recover the box (#4188)."""
        seed_default_presets_and_schedules()
        entries = Mode.objects.get(name="off").entries
        assert {loop for loop, value in entries.items() if value} == set(LOAD_BEARING_LOOPS)
        assert set(entries) == {spec.name for spec in DEFAULT_LOOPS}

    def test_low_token_keeps_only_deterministic_local_loops(self) -> None:
        seed_default_presets_and_schedules()
        entries = Mode.objects.get(name="low-token").entries
        assert entries["inbox"] is True
        assert entries["housekeeping"] is True
        assert entries["review"] is False
        assert entries["dispatch"] is False

    def test_maintenance_drains_in_flight_work_and_takes_no_new_intake(self) -> None:
        """The #4202 redefinition: finish and merge what is in flight, claim nothing new."""
        seed_default_presets_and_schedules()
        entries = Mode.objects.get(name="maintenance").entries
        assert entries["ship"] is True
        assert entries["review"] is True
        assert entries["tickets"] is False
        assert entries["issue_implementer"] is False

    def test_away_is_not_present_under_another_name(self) -> None:
        """#4202's open question: the two intake-taking presets are genuinely different.

        ``away`` masks the sole colleague-facing loop OFF and leaves the two self-QA
        loops inheriting their own flag rather than forcing them on.
        """
        seed_default_presets_and_schedules()
        present = Mode.objects.get(name="present").entries
        away = Mode.objects.get(name="away").entries
        assert {loop for loop in present if present[loop] != away.get(loop)} == {
            "followup",
            "eval_local",
            "dogfood",
        }
        assert away["followup"] is False
        assert "eval_local" not in away

    def test_a_freshly_seeded_away_resolves_through_an_override(self) -> None:
        """The seed → override → resolve chain lands on the row the operator named."""
        seed_default_presets_and_schedules()
        ModeOverride.objects.set_override("away")

        resolved = resolve_active_mode()

        assert resolved.name == "away"
        assert resolved.state_for("followup") is False

    def test_destructive_loops_inherit_in_present(self) -> None:
        seed_default_presets_and_schedules()
        entries = Mode.objects.get(name="present").entries
        for name in ("issue_implementer", "backlog_sweep", "outer_loop", "directive_loop"):
            assert name not in entries

    def test_standard_schedule_has_the_owner_working_hours_slots(self) -> None:
        seed_default_presets_and_schedules()
        standard = ModeSchedule.objects.get(name="standard")
        slots = {(tuple(sorted(slot.weekdays)), slot.start_time, slot.preset_name) for slot in standard.slots.all()}
        assert slots == {
            ((0, 1, 2, 3, 4), dt.time(9, 0), "present"),
            ((0, 1, 2, 3, 4), dt.time(16, 0), "away"),
            ((5, 6), dt.time(0, 0), "away"),
        }

    def test_standard_schedule_uses_the_vienna_timezone(self) -> None:
        seed_default_presets_and_schedules()
        assert ModeSchedule.objects.get(name="standard").timezone == "Europe/Vienna"

    def test_a_failed_slot_write_leaves_no_slotless_schedule_a_reseed_can_never_fill(self) -> None:
        # Slots are materialised for a NEWLY-created schedule only, so a schedule that
        # committed without them is a permanently empty calendar: every later seed sees
        # ``made=False`` and skips the slots for good.
        with (
            patch.object(ModeScheduleSlot.objects, "bulk_create", side_effect=OperationalError("disk I/O")),
            pytest.raises(OperationalError),
        ):
            seed_default_presets_and_schedules()

        assert not ModeSchedule.objects.filter(name="standard").exists()

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
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "always-away")
        seed_default_presets_and_schedules()
        assert ConfigSetting.objects.get_effective(ACTIVE_SCHEDULE_SETTING) == "always-away"

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

    Attended (``present``) Mon-Fri 09:00-16:00; every other hour is ``away``. Slots are
    wall-clock in Europe/Vienna, so the same Sat 12:00 resolves ``away`` across the DST
    boundary (summer CEST UTC+2 and winter CET UTC+1) with no hardcoded offset.
    """

    def setUp(self) -> None:
        Mode.objects.all().delete()
        ModeSchedule.objects.all().delete()
        seed_default_presets_and_schedules()

    def _active_at(self, moment: dt.datetime) -> str | None:
        active = resolve_active_preset(now=moment)
        return active.preset.name if active is not None else None

    def test_weekday_working_hours_resolve_present(self) -> None:
        # 2026-07-14 is a Tuesday (summer, CEST UTC+2).
        assert self._active_at(dt.datetime(2026, 7, 14, 10, 0, tzinfo=_VIENNA)) == "present"

    def test_weekday_evening_resolves_away(self) -> None:
        assert self._active_at(dt.datetime(2026, 7, 14, 22, 0, tzinfo=_VIENNA)) == "away"

    def test_weekday_early_morning_resolves_away(self) -> None:
        assert self._active_at(dt.datetime(2026, 7, 14, 7, 0, tzinfo=_VIENNA)) == "away"

    def test_summer_saturday_resolves_away(self) -> None:
        # 2026-07-18 is a Saturday under CEST (UTC+2).
        assert self._active_at(dt.datetime(2026, 7, 18, 12, 0, tzinfo=_VIENNA)) == "away"

    def test_winter_saturday_resolves_away_across_the_dst_boundary(self) -> None:
        # 2026-01-17 is a Saturday under CET (UTC+1) — the DST counterpart of the summer case.
        assert self._active_at(dt.datetime(2026, 1, 17, 12, 0, tzinfo=_VIENNA)) == "away"

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
    "present": (
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
    "away": (
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
                "ship",
                "review",
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
        frozenset({"tickets", "issue_implementer", "followup", "audit"}),
    ),
    "low-token": (
        frozenset({"inbox", "idle_stack_reaper", "local_stack_queue", "resource_pressure", "housekeeping"}),
        frozenset(spec.name for spec in DEFAULT_LOOPS)
        - frozenset({"inbox", "idle_stack_reaper", "local_stack_queue", "resource_pressure", "housekeeping"}),
    ),
    "off": (
        frozenset(LOAD_BEARING_LOOPS),
        frozenset(spec.name for spec in DEFAULT_LOOPS) - frozenset(LOAD_BEARING_LOOPS),
    ),
}


class TestShippedSpecsAreUnchangedByTheMoveIntoTheFile:
    """The relocation into ``defaults.toml`` retunes nothing — every mask holds."""

    def test_every_mode_ships_its_recorded_mask(self) -> None:
        by_name = {spec.name: spec.entries for spec in default_preset_specs()}
        assert set(by_name) == set(_SHIPPED_MASKS)
        for name, (on, off) in _SHIPPED_MASKS.items():
            entries = by_name[name]
            assert {loop for loop, value in entries.items() if value} == on, name
            assert {loop for loop, value in entries.items() if not value} == off, name

    def test_the_exhaustive_modes_name_every_shipped_loop(self) -> None:
        # `low-token` / `off` used to be built programmatically over every seed spec, so a
        # new loop was covered automatically. As shipped DATA they must name each loop
        # explicitly — an omitted one would silently INHERIT instead of being masked off.
        shipped = {spec.name for spec in DEFAULT_LOOPS}
        for name in ("low-token", "off"):
            entries = next(spec.entries for spec in default_preset_specs() if spec.name == name)
            assert set(entries) == shipped, name

    def test_always_away_is_one_all_week_slot(self) -> None:
        holiday = next(spec for spec in default_schedule_specs() if spec.name == "always-away")
        assert holiday.timezone == ""
        assert [(slot.days, slot.start_time, slot.preset_name) for slot in holiday.slots] == [
            ([0, 1, 2, 3, 4, 5, 6], dt.time(0, 0), "away")
        ]


class TestNoShippedModeFillsWhatItCannotDrain:
    """A shipped mode masking delivery off must not leave intake admitted (#4096).

    ``maintenance`` masked ``tickets`` / ``ship`` off but named no opinion on
    ``issue_implementer``, so it inherited ``Loop.enabled`` and kept claiming issues
    overnight that nothing could merge. It now names both, and ``off`` / ``low-token``
    name every loop.
    """

    def test_no_shipped_mode_masks_delivery_while_admitting_intake(self) -> None:
        """Judged on a box that RUNS the factory.

        The intake loop's own flag is the operator's switch, so a shipped mask must express
        the intent rather than lean on that switch happening to be off.
        """
        running = dict.fromkeys(INTAKE_LOOPS, True)
        offenders = {
            spec.name: found.detail
            for spec in default_preset_specs()
            if (found := intake_without_delivery(spec.entries, base_enabled=running)) is not None
        }

        assert offenders == {}, offenders


class TestSpecsAreShippedDataNotCode:
    """The mode / schedule specs are built from the shipped ``defaults.toml`` tables."""

    def test_mode_specs_are_loaded_from_the_file_they_are_pointed_at(self, tmp_path: Path) -> None:
        fixture = tmp_path / "defaults.toml"
        fixture.write_text(
            "[modes.sentinel]\n"
            'description = "a synthetic mode"\n'
            "[modes.sentinel.entries]\n"
            "inbox = true\n"
            "dispatch = false\n",
            encoding="utf-8",
        )
        (spec,) = default_preset_specs(fixture)
        assert spec == PresetSpec(
            name="sentinel", description="a synthetic mode", entries={"inbox": True, "dispatch": False}
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
            'preset_name = "present"\n',
            encoding="utf-8",
        )
        (spec,) = default_schedule_specs(fixture)
        assert spec == ScheduleSpec(
            name="sentinel",
            description="a synthetic calendar",
            slots=(SlotSpec(days=[2, 3], start_time=dt.time(7, 15), preset_name="present"),),
            timezone="UTC",
        )

    def test_an_omitted_optional_field_falls_back_to_the_dataclass_default(self, tmp_path: Path) -> None:
        fixture = tmp_path / "defaults.toml"
        fixture.write_text(
            '[modes.sentinel]\ndescription = "x"\n[schedules.cal]\ndescription = "y"\n', encoding="utf-8"
        )
        (mode,) = default_preset_specs(fixture)
        (schedule,) = default_schedule_specs(fixture)
        assert mode.entries == {}
        assert (schedule.slots, schedule.timezone) == ((), "")

    def test_every_shipped_mode_and_schedule_name_matches_the_file(self) -> None:
        assert {spec.name for spec in default_preset_specs()} == set(shipped_seed_table("modes"))
        assert {spec.name for spec in default_schedule_specs()} == set(shipped_seed_table("schedules"))


class TestNoShippedModeConsumesWhatItCannotReclaim:
    """No mask may keep the backup writing once every reclaim loop is quiet (#4188).

    Judged against the shipped ``[loops]`` flags, so an absent entry resolves the way a
    fresh box resolves it. Asserted over EVERY mode rather than the one that had the bug,
    because the point is that a future mode cannot reintroduce the shape.
    """

    def test_no_shipped_mode_admits_the_backup_over_a_quiet_reclaim_pair(self) -> None:
        base = {spec.name: spec.default_enabled for spec in load_loop_specs()}
        offenders = {
            spec.name: found.detail
            for spec in default_preset_specs()
            if (found := backup_without_reclaim(spec.entries, base_enabled=base)) is not None
        }

        assert offenders == {}, offenders

    def test_only_the_low_token_mode_may_quiet_the_load_bearing_tier(self) -> None:
        offenders = {
            spec.name: quieted_load_bearing(spec.entries)
            for spec in default_preset_specs()
            if spec.name != DEFAULT_LOW_POWER_PRESET and quieted_load_bearing(spec.entries)
        }

        assert offenders == {}, offenders

    def test_the_halt_mode_forces_the_tier_on_rather_than_leaning_on_the_column(self) -> None:
        """``off`` must ADMIT the tier, not merely decline to mask it — the column may be off."""
        specs = {spec.name: spec for spec in default_preset_specs()}

        assert all(specs["off"].entries.get(loop) is True for loop in LOAD_BEARING_LOOPS)


class TestTheCollapseMigrationsReplacementTextMatchesWhatShips:
    """The collapse's REPLACEMENT text is what a refreshed row ends up carrying.

    ``0071`` rewrites a description only while the row still holds the SHIPPED text, so
    drift between its replacement and ``defaults.toml`` leaves a live box's wording
    permanently behind the shipped table with nothing failing — the two files had no
    link at all until this test.
    """

    @staticmethod
    def _collapse():
        return import_module("teatree.core.migrations.0071_collapse_modes_to_five_presets")

    def test_every_replacement_description_equals_the_shipped_mode_description(self) -> None:
        shipped = {name: entry["description"] for name, entry in shipped_seed_table("modes").items()}

        drift = {
            name: (replacement, shipped.get(name))
            for name, (_, replacement) in self._collapse()._DESCRIPTIONS.items()
            if shipped.get(name) != replacement
        }

        assert drift == {}, drift

    def test_the_replacement_schedule_description_equals_the_shipped_one(self) -> None:
        _, replacement = self._collapse()._SCHEDULE_DESCRIPTIONS
        _, new_name = self._collapse()._SCHEDULE_RENAME

        assert shipped_seed_table("schedules")[new_name]["description"] == replacement
