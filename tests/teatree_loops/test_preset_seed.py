"""Idempotent seed of the default presets + schedules (#3159, #4202).

``t3 setup`` seeds the 5 curated presets and the ``standard`` / ``always-away``
schedules as owner-editable DB data. ``standard`` ships as the active schedule (owner
working hours, Europe/Vienna), pinned through the provenance-aware
``ConfigSetting.seed`` so an operator switch is never clobbered. Integration-first
against the real DB.
"""

import datetime as dt
import io
import itertools
import zoneinfo
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

import django.test
import pytest
from django.core.management import call_command
from django.db.utils import OperationalError

from teatree.config.seed_defaults import shipped_seed_table
from teatree.core.mode_resolution import owner_voice_forbidden, resolve_active_mode
from teatree.core.models import ConfigSetting, Loop, Mode, ModeOverride, ModeSchedule, ModeScheduleSlot
from teatree.core.models.config_setting import ENTRYPOINT_SEEDER
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING, resolve_active_preset
from teatree.loops.base import LoopDeterminism
from teatree.loops.mode_shape import backup_without_reclaim
from teatree.loops.preset_seed import (
    PresetSpec,
    ScheduleSpec,
    SlotSpec,
    default_preset_specs,
    default_schedule_specs,
    seed_default_presets_and_schedules,
)
from teatree.loops.registry import iter_loops
from teatree.loops.seed import DEFAULT_LOOPS

_EXPECTED_PRESETS = {"present", "afk", "maintenance", "token-outage", "off"}
_EXPECTED_SCHEDULES = {"standard", "always-afk"}
#: Names earlier collapses retired. Seeding one again would resurrect a dead posture.
_RETIRED_PRESETS = {"engaged", "heads-down", "low-power", "unattended", "offline", "away", "low-token", "factory-solo"}
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

    def test_off_runs_nothing_at_all(self) -> None:
        """B3: a real off. What it strands, the switch reports — it no longer refuses to stop."""
        seed_default_presets_and_schedules()
        entries = Mode.objects.get(name="off").entries
        assert set(entries) == {spec.name for spec in DEFAULT_LOOPS}
        assert not any(entries.values())

    def test_token_outage_keeps_only_the_loops_that_never_call_a_model(self) -> None:
        seed_default_presets_and_schedules()
        entries = Mode.objects.get(name="token-outage").entries
        assert entries["housekeeping"] is True
        assert entries["resource_pressure"] is True
        # ``inbox`` routes through an agent, so a token outage stops it too — the box is
        # then reachable only out of band, which is the cost the posture exists to pay.
        assert entries["inbox"] is False
        assert entries["review"] is False

    def test_maintenance_is_self_repair_with_no_delivery_and_no_voice(self) -> None:
        seed_default_presets_and_schedules()
        row = Mode.objects.get(name="maintenance")
        assert row.entries["ci_eval_heal"] is True
        assert row.entries["db_backup"] is True
        assert row.entries["ship"] is False
        assert row.entries["review"] is False
        assert row.entries["tickets"] is False
        assert row.forbids_egress

    def test_afk_does_the_daily_job_without_a_voice(self) -> None:
        """B6: everything runs except directive interpretation; nothing goes out on the owner's behalf."""
        seed_default_presets_and_schedules()
        row = Mode.objects.get(name="afk")
        assert {loop for loop, value in row.entries.items() if not value} == {"directive_loop"}
        assert row.forbids_egress

    def test_present_acts_on_the_owners_behalf(self) -> None:
        seed_default_presets_and_schedules()
        assert not Mode.objects.get(name="present").forbids_egress

    def test_a_freshly_seeded_afk_resolves_through_an_override(self) -> None:
        """The seed → override → resolve chain lands on the row the operator named."""
        seed_default_presets_and_schedules()
        ModeOverride.objects.set_override("afk", reason="test override")

        resolved = resolve_active_mode()

        assert resolved.name == "afk"
        assert resolved.state_for("followup") is True

    def test_present_runs_every_loop(self) -> None:
        """B6: "do everything". The subtraction is ``afk``'s, and it is one loop plus egress."""
        seed_default_presets_and_schedules()
        assert all(Mode.objects.get(name="present").entries.values())

    def test_standard_schedule_has_the_owner_working_hours_slots(self) -> None:
        seed_default_presets_and_schedules()
        standard = ModeSchedule.objects.get(name="standard")
        slots = {(tuple(sorted(slot.weekdays)), slot.start_time, slot.preset_name) for slot in standard.slots.all()}
        assert slots == {
            ((0, 1, 2, 3, 4), dt.time(9, 0), "present"),
            ((0, 1, 2, 3, 4), dt.time(16, 0), "afk"),
            ((5, 6), dt.time(0, 0), "afk"),
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

    def test_the_shipped_pin_is_written_with_its_seed_provenance(self) -> None:
        """`seeded_by` + `seed_value` are what let a later reseed tell its own row from a pin.

        This is the ONE call site that writes them, so without this assertion the two
        columns can go unpopulated with nothing failing — the live control DB carries 64
        rows and not one of them has either.
        """
        seed_default_presets_and_schedules()

        row = ConfigSetting.objects.get(key=ACTIVE_SCHEDULE_SETTING)
        assert row.seeded_by == ENTRYPOINT_SEEDER
        assert row.seed_value == row.value

    def test_an_operator_written_pin_is_never_adopted_by_a_later_seed(self) -> None:
        # The control for the assertion above: a row the seeder does not own must stay
        # provenance-free, so a passing `seeded_by` cannot come from the seeder claiming
        # whatever it finds.
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "always-away")

        seed_default_presets_and_schedules()

        row = ConfigSetting.objects.get(key=ACTIVE_SCHEDULE_SETTING)
        assert row.seeded_by == ""
        assert row.seed_value is None

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

    def test_weekday_evening_resolves_afk(self) -> None:
        assert self._active_at(dt.datetime(2026, 7, 14, 22, 0, tzinfo=_VIENNA)) == "afk"

    def test_weekday_early_morning_resolves_afk(self) -> None:
        assert self._active_at(dt.datetime(2026, 7, 14, 7, 0, tzinfo=_VIENNA)) == "afk"

    def test_summer_saturday_resolves_afk(self) -> None:
        # 2026-07-18 is a Saturday under CEST (UTC+2).
        assert self._active_at(dt.datetime(2026, 7, 18, 12, 0, tzinfo=_VIENNA)) == "afk"

    def test_winter_saturday_resolves_afk_across_the_dst_boundary(self) -> None:
        # 2026-01-17 is a Saturday under CET (UTC+1) — the DST counterpart of the summer case.
        assert self._active_at(dt.datetime(2026, 1, 17, 12, 0, tzinfo=_VIENNA)) == "afk"

    def test_management_command_reports_creates(self) -> None:
        Mode.objects.all().delete()
        ModeSchedule.objects.all().delete()
        out = io.StringIO()
        call_command("seed_loops", stdout=out)
        assert "presets:" in out.getvalue()


#: The shipped posture of every mode as (ON, OFF) loop names — every loop named by both
#: sets together, because a preset holds no partial opinions (B1). Pinned here so a
#: retune of one loop in ``defaults.toml`` is a reviewed change rather than a whim.
_ALL_LOOPS = frozenset(spec.name for spec in DEFAULT_LOOPS)
_PRESENT_ON = _ALL_LOOPS
_TOKEN_OUTAGE_ON = frozenset(loop.name for loop in iter_loops() if loop.determinism is LoopDeterminism.DETERMINISTIC)
_MAINTENANCE_ON = frozenset(
    {
        "ci_eval_heal",
        "db_backup",
        "dispatch",
        "housekeeping",
        "idle_stack_reaper",
        "inbox",
        "local_stack_queue",
        "resource_pressure",
        "snapshot_warmer",
    }
)
_AFK_OFF = frozenset({"directive_loop"})

_SHIPPED_MASKS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "present": (_PRESENT_ON, _ALL_LOOPS - _PRESENT_ON),
    "afk": (_ALL_LOOPS - _AFK_OFF, _AFK_OFF),
    "maintenance": (_MAINTENANCE_ON, _ALL_LOOPS - _MAINTENANCE_ON),
    "token-outage": (_TOKEN_OUTAGE_ON, _ALL_LOOPS - _TOKEN_OUTAGE_ON),
    "off": (frozenset(), _ALL_LOOPS),
}

#: The postures ordered by how much they run, widest first (B13). ``token-outage`` is NOT a
#: member: it is defined by a property, not by a position in the ordering.
_POSTURE_CHAIN: tuple[str, ...] = ("present", "afk", "maintenance", "off")

#: Which shipped postures refuse to act outward on the owner's behalf (B6).
_SHIPPED_EGRESS_FORBIDDEN = frozenset({"afk", "maintenance"})

#: Two moments the shipped ``standard`` calendar answers differently — inside the owner's
#: Mon-Fri 09:00-16:00 window and outside it.
_WEDNESDAY_WORKING_HOURS = dt.datetime(2026, 9, 9, 10, 0, tzinfo=_VIENNA)
_WEDNESDAY_NIGHT = dt.datetime(2026, 9, 9, 22, 0, tzinfo=_VIENNA)


def _shipped_masks() -> dict[str, dict[str, bool]]:
    return {spec.name: spec.entries for spec in default_preset_specs()}


class TestShippedSpecsCarryTheOwnersPostures:
    """Every mask holds, and every posture declares its egress opinion explicitly."""

    def test_every_mode_ships_its_recorded_mask(self) -> None:
        by_name = _shipped_masks()
        assert set(by_name) == set(_SHIPPED_MASKS)
        for name, (on, off) in _SHIPPED_MASKS.items():
            entries = by_name[name]
            assert {loop for loop, value in entries.items() if value} == on, name
            assert {loop for loop, value in entries.items() if not value} == off, name

    def test_every_mode_names_every_shipped_loop(self) -> None:
        """A loop a mask omits reads OFF, which is the fail-safe answer rather than a chosen one."""
        for spec in default_preset_specs():
            assert set(spec.entries) == _ALL_LOOPS, spec.name

    def test_each_posture_in_the_chain_is_a_superset_of_the_next(self) -> None:
        """B13: ``present >= afk >= maintenance >= off`` — stepping down only ever REMOVES work.

        Asserted rather than left to the seed data, because seed data that merely happens to
        satisfy the order is exactly how it broke: ``present`` was written from this box's
        ``default_enabled`` column, which made it the second most restrictive posture after
        ``off``, so a present -> afk switch ADDED eighteen loops including ``ship`` and
        ``review``. ``token-outage`` is deliberately outside the chain — it is defined by a
        property (a loop needs no tokens), not by a position in the ordering.
        """
        on = {name: {loop for loop, runs in entries.items() if runs} for name, entries in _shipped_masks().items()}

        for wider, narrower in itertools.pairwise(_POSTURE_CHAIN):
            assert on[narrower] <= on[wider], (
                f"{narrower} runs what {wider} does not: {sorted(on[narrower] - on[wider])}"
            )

    def test_token_outage_runs_exactly_the_loops_that_never_call_a_model(self) -> None:
        """B13: ``token-outage`` skips every loop that USES AI — derived, never hand-listed.

        The registry already answers it (``MiniLoop.determinism``), so the shipped table is
        held to that answer rather than to a list someone maintains: a loop added later is
        classified where it is defined, and this turns RED if the two drift.
        """
        deterministic = {loop.name for loop in iter_loops() if loop.determinism is LoopDeterminism.DETERMINISTIC}
        on = {loop for loop, runs in _shipped_masks()["token-outage"].items() if runs}

        assert on == deterministic, {"wrongly on": sorted(on - deterministic), "missing": sorted(deterministic - on)}

    def test_the_forbidding_postures_say_so_rather_than_leaving_it_inferred(self) -> None:
        forbidding = {spec.name for spec in default_preset_specs() if spec.egress == "forbid"}
        assert forbidding == _SHIPPED_EGRESS_FORBIDDEN

    def test_always_afk_is_one_all_week_slot(self) -> None:
        holiday = next(spec for spec in default_schedule_specs() if spec.name == "always-afk")
        assert holiday.timezone == ""
        assert [(slot.days, slot.start_time, slot.preset_name) for slot in holiday.slots] == [
            ([0, 1, 2, 3, 4, 5, 6], dt.time(0, 0), "afk")
        ]


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestTheSeededPosturesDecideTheOwnersVoice(django.test.TestCase):
    """What ``egress`` DOES once seeded, executed through the chokepoint an operator acts on.

    ``TestShippedSpecsCarryTheOwnersPostures`` reads the column; this selects each seeded
    posture and asks :func:`owner_voice_forbidden`, so a posture DOCUMENTED as closing the
    owner's voice that does not close it turns this RED instead of surviving in prose.
    """

    def setUp(self) -> None:
        ModeOverride.objects.all().delete()
        Mode.objects.all().delete()
        ModeSchedule.objects.all().delete()
        seed_default_presets_and_schedules()

    def test_exactly_the_shipped_forbidding_postures_close_the_owners_voice(self) -> None:
        refusing = set()
        for name in Mode.objects.values_list("name", flat=True):
            ModeOverride.objects.set_override(name, reason="posture under test")
            if owner_voice_forbidden():
                refusing.add(name)

        assert refusing == _SHIPPED_EGRESS_FORBIDDEN

    def test_a_fresh_install_leaves_colleague_egress_open_inside_working_hours(self) -> None:
        """The disclosed reversal, executed: ``t3 setup`` alone opens the owner's voice."""
        assert resolve_active_mode(_WEDNESDAY_WORKING_HOURS).name == "present"
        assert owner_voice_forbidden(_WEDNESDAY_WORKING_HOURS) is False

    def test_a_fresh_install_closes_it_again_outside_them(self) -> None:
        assert resolve_active_mode(_WEDNESDAY_NIGHT).name == "afk"
        assert owner_voice_forbidden(_WEDNESDAY_NIGHT) is True


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
        assert mode.egress == "allow"
        assert (schedule.slots, schedule.timezone) == ((), "")

    def test_every_shipped_mode_and_schedule_name_matches_the_file(self) -> None:
        assert {spec.name for spec in default_preset_specs()} == set(shipped_seed_table("modes"))
        assert {spec.name for spec in default_schedule_specs()} == set(shipped_seed_table("schedules"))


class TestNoShippedModeConsumesWhatItCannotReclaim:
    """No mask may keep the backup writing once every reclaim loop is quiet (B4).

    Asserted over EVERY mode rather than the one that had the bug, because the point is
    that a future mode cannot reintroduce the shape.
    """

    def test_no_shipped_mode_admits_the_backup_over_a_quiet_reclaim_pair(self) -> None:
        offenders = {
            spec.name: found.detail
            for spec in default_preset_specs()
            if (found := backup_without_reclaim(spec.entries)) is not None
        }

        assert offenders == {}, offenders


class TestThePostureMigrationsReplacementTextMatchesWhatShips:
    """What ``0086`` writes onto a live row is what ``defaults.toml`` ships.

    The migration rewrites descriptions unconditionally, so drift between the two leaves
    a live box's wording permanently apart from the shipped table with nothing failing.
    """

    @staticmethod
    def _postures():
        return import_module("teatree.core.migrations.0086_total_presets_and_the_five_postures")

    def test_every_written_description_equals_the_shipped_mode_description(self) -> None:
        shipped = {name: entry["description"] for name, entry in shipped_seed_table("modes").items()}

        drift = {
            name: (written, shipped.get(name))
            for name, written in self._postures()._DESCRIPTIONS.items()
            if shipped.get(name) != written
        }

        assert drift == {}, drift

    def test_the_written_schedule_description_equals_the_shipped_one(self) -> None:
        postures = self._postures()

        assert shipped_seed_table("schedules")[postures._SCHEDULE]["description"] == postures._SCHEDULE_DESCRIPTION

    def test_the_masks_it_writes_satisfy_the_posture_chain(self) -> None:
        """B13 holds on a LIVE box too — the migration lands every posture in the chain itself."""
        postures = self._postures()
        loops = set(shipped_seed_table("loops"))
        landed = {
            "present": loops,
            "afk": loops - set(postures._AFK_OFF),
            "maintenance": set(postures._MAINTENANCE_ON),
            "off": set(),
        }

        for wider, narrower in itertools.pairwise(_POSTURE_CHAIN):
            assert landed[narrower] <= landed[wider], sorted(landed[narrower] - landed[wider])

    def test_the_maintenance_and_afk_masks_it_writes_equal_the_shipped_tables(self) -> None:
        postures = self._postures()
        shipped = {name: entry["entries"] for name, entry in shipped_seed_table("modes").items()}

        assert {loop for loop, on in shipped["maintenance"].items() if on} == set(postures._MAINTENANCE_ON)
        assert {loop for loop, on in shipped["afk"].items() if not on} == set(postures._AFK_OFF)


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestSeedNamesEveryLiveLoop(django.test.TestCase):
    """The seed is a write seam like any other, so it may not create a preset with a gap.

    Every other seam folds through ``totalized_entries``. This one wrote the shipped table
    verbatim, so a live loop the shipped ``[modes]`` tables never named — an overlay's own
    loop, or one added to ``DEFAULT_LOOPS`` ahead of the tables — was seeded ABSENT rather
    than off: read OFF by ``Mode.state_for`` but refused by ``Mode.clean``, which locks the
    row out of the one surface (django-admin) that could repair it.
    """

    def setUp(self) -> None:
        ModeOverride.objects.all().delete()
        Mode.objects.all().delete()
        ModeSchedule.objects.all().delete()

    def test_a_loop_the_shipped_tables_never_named_is_seeded_off_in_every_preset(self) -> None:
        Loop.objects.create(name="overlay_only", script="overlay/tick.py", delay_seconds=300)

        seed_default_presets_and_schedules()

        seeded = {preset.name: preset.entries.get("overlay_only") for preset in Mode.objects.all()}
        assert seeded == dict.fromkeys(_EXPECTED_PRESETS, False)

    def test_every_seeded_preset_holds_an_opinion_on_every_live_loop(self) -> None:
        Loop.objects.create(name="overlay_only", script="overlay/tick.py", delay_seconds=300)
        live = set(Loop.objects.values_list("name", flat=True))

        seed_default_presets_and_schedules()

        for preset in Mode.objects.all():
            assert set(preset.entries) == live, preset.name

    def test_seeding_before_any_loop_exists_keeps_the_shipped_opinions(self) -> None:
        """Totalizing against an empty loop table would drop every shipped opinion.

        ``present`` seeded as ``{}`` runs nothing, and the ``post_save`` backfill then
        writes each loop in as ``False`` — a box that ships "do everything" doing none of it.
        """
        Loop.objects.all().delete()

        seed_default_presets_and_schedules()

        assert Mode.objects.get(name="present").entries == _shipped_masks()["present"]
