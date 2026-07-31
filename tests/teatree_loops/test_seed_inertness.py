"""A shipped loop/preset/schedule that is absent, off, or not ticking is NAMED (#3842).

Ten of twenty-seven shipped loops sat present and inert on the live box while every
surface reported the fleet healthy; nothing had ever been deleted. So the report sources
its EXPECTED set from the shipped seed rather than from the DB — a check that reads the DB
for both sides cannot see a missing row, the same self-referential defect as a golden
compared against its own renderer (#3836).

Every removal case carries its CONTROL (the same name, row present, absent from the
findings). Without it the suite cannot tell "detects the removal" from "reports everything
always", which is exactly the vacuous pass the ticket's acceptance forbids.
"""

import datetime as dt
import tempfile
from pathlib import Path

import django.test
from django.utils import timezone

from teatree.core.mode_resolution import set_mode_override
from teatree.core.models import ConfigSetting, Loop, Mode, ModeSchedule, ModeScheduleSlot
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING
from teatree.loops.preset_seed import seed_default_presets_and_schedules
from teatree.loops.seed import seed_default_loops_and_prompts
from teatree.loops.seed_inertness import (
    KIND_DANGLING_SLOT,
    KIND_DISABLED,
    KIND_DISABLED_VS_SHIPPED,
    KIND_EMPTY,
    KIND_EMPTY_MASK,
    KIND_INACTIVE,
    KIND_MISSING,
    KIND_STALE,
    KIND_SUPPRESSED,
    shipped_inertness,
)

_GHOST_TOML = """
[loops.ghost_loop]
delay_seconds = 300
default_enabled = true
description = "a loop that ships but was never seeded"

[modes.ghost_preset]
description = "a preset that ships but was never seeded"

[schedules.ghost_schedule]
description = "a schedule that ships but was never seeded"
"""


def _fixture_toml(body: str) -> Path:
    """A throwaway shipped-seed file, so the report can be pointed away from the packaged one."""
    path = Path(tempfile.mkdtemp()) / "shipped.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _named(findings: tuple, family: str, name: str) -> list:
    return [f for f in findings if f.family == family and f.name == name]


def _kinds(findings: tuple, family: str, name: str) -> list[str]:
    return [f.kind for f in _named(findings, family, name)]


class TestShippedRemovalIsDetected(django.test.TestCase):
    """The acceptance case: remove a seeded row, the report names it — with the control."""

    def setUp(self) -> None:
        seed_default_loops_and_prompts()
        seed_default_presets_and_schedules()

    def test_a_deleted_loop_row_is_named_missing(self) -> None:
        assert KIND_MISSING not in _kinds(shipped_inertness(), "loop", "review"), "control: present row is not missing"

        Loop.objects.filter(name="review").delete()

        found = _named(shipped_inertness(), "loop", "review")
        assert [f.kind for f in found] == [KIND_MISSING]
        assert found[0].is_fault

    def test_a_deleted_preset_row_is_named_missing(self) -> None:
        assert KIND_MISSING not in _kinds(shipped_inertness(), "preset", "engaged"), (
            "control: present row is not missing"
        )

        Mode.objects.filter(name="engaged").delete()

        found = _named(shipped_inertness(), "preset", "engaged")
        assert [f.kind for f in found] == [KIND_MISSING]
        assert found[0].is_fault

    def test_a_deleted_schedule_row_is_named_missing(self) -> None:
        control = _kinds(shipped_inertness(), "schedule", "standard")
        assert KIND_MISSING not in control, "control: present row is not missing"

        ModeSchedule.objects.filter(name="standard").delete()

        found = _named(shipped_inertness(), "schedule", "standard")
        assert [f.kind for f in found] == [KIND_MISSING]
        assert found[0].is_fault

    def test_the_missing_detail_names_what_stopped_happening(self) -> None:
        shipped = Loop.objects.get(name="review").description
        Loop.objects.filter(name="review").delete()

        detail = _named(shipped_inertness(), "loop", "review")[0].detail

        assert shipped[:40] in detail, "the operator must learn what the deleted loop did"
        assert "t3 setup" in detail, "deletion self-heals — say so"


class TestExpectedSetComesFromTheSeed(django.test.TestCase):
    """Sourcing from the seed, not the DB — a DB-sourced report cannot fail this."""

    def test_a_shipped_name_with_no_db_row_at_all_is_reported(self) -> None:
        findings = shipped_inertness(path=_fixture_toml(_GHOST_TOML))

        assert [f.kind for f in _named(findings, "loop", "ghost_loop")] == [KIND_MISSING]
        assert [f.kind for f in _named(findings, "preset", "ghost_preset")] == [KIND_MISSING]
        assert [f.kind for f in _named(findings, "schedule", "ghost_schedule")] == [KIND_MISSING]


class TestDisabledSeverityUsesTheShippedFlag(django.test.TestCase):
    """Ten shipped-off loops are not ten faults — only a shipped-ON loop turned off is."""

    def setUp(self) -> None:
        seed_default_loops_and_prompts()

    def test_a_shipped_on_loop_found_off_is_a_fault(self) -> None:
        Loop.objects.filter(name="inbox").update(enabled=False)

        found = _named(shipped_inertness(), "loop", "inbox")

        assert [f.kind for f in found] == [KIND_DISABLED_VS_SHIPPED]
        assert found[0].is_fault

    def test_a_shipped_off_loop_found_off_is_only_a_note(self) -> None:
        Loop.objects.filter(name="dogfood").update(enabled=False)

        found = _named(shipped_inertness(), "loop", "dogfood")

        assert [f.kind for f in found] == [KIND_DISABLED]
        assert not found[0].is_fault


class TestStaleLoopsSplitOnWhetherAnythingExplainsThem(django.test.TestCase):
    def setUp(self) -> None:
        seed_default_loops_and_prompts()
        seed_default_presets_and_schedules()
        self.now = timezone.now()
        Loop.objects.filter(name="inbox").update(enabled=True, last_run_at=self.now - dt.timedelta(days=2))

    def test_an_unexplained_stale_loop_is_a_fault(self) -> None:
        set_mode_override("engaged")

        found = _named(shipped_inertness(now=self.now), "loop", "inbox")

        assert [f.kind for f in found] == [KIND_STALE]
        assert found[0].is_fault

    def test_a_masked_stale_loop_is_only_a_note(self) -> None:
        Mode.objects.create(name="inbox-off", entries={"inbox": False}, description="test mask")
        set_mode_override("inbox-off")

        found = _named(shipped_inertness(now=self.now), "loop", "inbox")

        assert [f.kind for f in found] == [KIND_SUPPRESSED]
        assert not found[0].is_fault


class TestPresetInertness(django.test.TestCase):
    def setUp(self) -> None:
        seed_default_presets_and_schedules()

    def test_a_hand_selectable_preset_nothing_references_is_not_reported(self) -> None:
        """4 of 7 shipped presets are unreferenced on a fresh box — by design, not inertness."""
        assert _named(shipped_inertness(), "preset", "heads-down") == []

    def test_a_preset_whose_mask_was_emptied_is_a_fault(self) -> None:
        Mode.objects.filter(name="engaged").update(entries={})

        found = _named(shipped_inertness(), "preset", "engaged")

        assert [f.kind for f in found] == [KIND_EMPTY_MASK]
        assert found[0].is_fault


class TestScheduleInertness(django.test.TestCase):
    def setUp(self) -> None:
        seed_default_presets_and_schedules()
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "standard")

    def test_the_active_schedule_with_no_slots_is_a_fault(self) -> None:
        ModeScheduleSlot.objects.filter(schedule__name="standard").delete()

        found = _named(shipped_inertness(), "schedule", "standard")

        assert [f.kind for f in found] == [KIND_EMPTY]
        assert found[0].is_fault

    def test_a_slot_naming_an_absent_preset_is_a_fault(self) -> None:
        ModeScheduleSlot.objects.filter(schedule__name="standard").update(preset_name="deleted-preset")

        found = _named(shipped_inertness(), "schedule", "standard")

        assert [f.kind for f in found] == [KIND_DANGLING_SLOT]
        assert found[0].is_fault
        assert "deleted-preset" in found[0].detail

    def test_a_schedule_that_is_not_the_active_one_is_only_a_note(self) -> None:
        found = _named(shipped_inertness(), "schedule", "always-unattended")

        assert [f.kind for f in found] == [KIND_INACTIVE]
        assert not found[0].is_fault


class TestAFreshlySeededBoxIsClean(django.test.TestCase):
    """A report that is noisy out of the box is one people learn to ignore."""

    def test_no_faults_after_the_shipped_seed_runs(self) -> None:
        seed_default_loops_and_prompts()
        seed_default_presets_and_schedules()
        set_mode_override("engaged")
        # `stale_loops` measures a never-run loop from `created_at`, so the migration-seeded
        # rows age past 3x their cadence as the suite runs; stamp the anchor rather than
        # inherit "the DB is young" from how long the suite has been going.
        Loop.objects.update(last_run_at=timezone.now())

        faults = [f for f in shipped_inertness(now=timezone.now()) if f.is_fault]

        assert faults == [], f"fresh install reports faults: {[f.label for f in faults]}"
