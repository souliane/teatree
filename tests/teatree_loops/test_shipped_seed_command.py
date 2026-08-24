"""``manage.py shipped_seed`` renders the audit and EXITS NON-ZERO on a fault (#3842).

The exit code is the load-bearing part and the easiest thing to get silently wrong: a
``typer.Exit`` raised inside a ``TyperCommand`` reached through ``call_command`` is
swallowed, so the process exits 0 and CI reports green on a real failure. These tests
assert ``SystemExit`` with its code, which is the only shape that survives the call chain.
"""

import datetime as dt
import io
import json
from unittest import mock

import django.test
import pytest
from django.core.management import call_command
from django.utils import timezone

from teatree.cli.doctor.app import _check_shipped_seed_inertness
from teatree.core.models import ConfigSetting, Loop, Mode, ModeSchedule, ModeScheduleSlot
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING
from teatree.loops.mode_shape import INTAKE_LOOPS
from teatree.loops.preset_seed import seed_default_presets_and_schedules
from teatree.loops.seed import seed_default_loops_and_prompts
from teatree.loops.shipped_guard import shipped_delete_phrase


def _fleet_just_ticked() -> None:
    """Stamp every loop's cadence anchor to now — the precondition a clean audit needs.

    ``stale_loops`` measures a loop that has NEVER run from ``created_at``, so the rows the
    migration seeded age past 3x their cadence as the suite runs and a clean-box assertion
    that relied on a young DB flips red purely on wall-clock. Setting the anchor states the
    precondition instead of inheriting it from how long the suite has been going.
    """
    Loop.objects.update(last_run_at=timezone.now())


class TestAuditExitCode(django.test.TestCase):
    def setUp(self) -> None:
        seed_default_loops_and_prompts()
        seed_default_presets_and_schedules()
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "standard")
        _fleet_just_ticked()

    def test_a_missing_shipped_row_exits_non_zero(self) -> None:
        Loop.objects.filter(name="review").delete()

        with pytest.raises(SystemExit) as caught:
            call_command("shipped_seed", "audit", stdout=io.StringIO(), stderr=io.StringIO())

        assert caught.value.code == 1

    def test_a_clean_box_exits_zero(self) -> None:
        err = io.StringIO()

        call_command("shipped_seed", "audit", stdout=io.StringIO(), stderr=err)

        assert "OK" in err.getvalue()

    def test_json_carries_every_finding_and_the_fault_count(self) -> None:
        Loop.objects.filter(name="review").delete()
        out = io.StringIO()

        with pytest.raises(SystemExit):
            call_command("shipped_seed", "audit", json_output=True, stdout=out, stderr=io.StringIO())

        payload = json.loads(out.getvalue())
        assert payload["fault_count"] >= 1
        assert any(f["name"] == "review" and f["kind"] == "missing" for f in payload["findings"])

    def test_a_deliberate_note_alone_does_not_fail_the_audit(self) -> None:
        """`always-away` is inactive by design — a note, never a non-zero exit."""
        err = io.StringIO()

        call_command("shipped_seed", "audit", stdout=io.StringIO(), stderr=err)

        assert "always-away" in err.getvalue(), "the note is still reported"

    def test_an_operator_override_is_reported_under_the_notes_block_and_exits_zero(self) -> None:
        """The report distinguishes never-seeded from deliberately overridden (#4096)."""
        ModeScheduleSlot.objects.create(
            schedule=ModeSchedule.objects.get(name="standard"),
            days=[0, 1, 2, 3, 4],
            start_time=dt.time(19, 0),
            preset_name="maintenance",
        )
        err = io.StringIO()

        call_command("shipped_seed", "audit", stdout=io.StringIO(), stderr=err)

        report = err.getvalue()
        assert "NOTES (deliberate — not failures)" in report
        assert "slots_overridden" in report
        assert "adds Mon,Tue,Wed,Thu,Fri 19:00 -> maintenance" in report

    def test_a_mode_that_masks_delivery_while_admitting_intake_exits_non_zero(self) -> None:
        """The 13h-a-night stall the audit used to report as OK (#4096)."""
        Loop.objects.filter(name__in=INTAKE_LOOPS).update(enabled=True)
        Mode.objects.filter(name="maintenance").update(entries={"ship": False, "tickets": False})
        err = io.StringIO()

        with pytest.raises(SystemExit) as caught:
            call_command("shipped_seed", "audit", stdout=io.StringIO(), stderr=err)

        assert caught.value.code == 1
        assert "intake_without_delivery" in err.getvalue()


class TestDeleteVerbs(django.test.TestCase):
    def setUp(self) -> None:
        seed_default_loops_and_prompts()
        seed_default_presets_and_schedules()
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "standard")

    def test_deleting_a_shipped_loop_without_the_phrase_exits_non_zero(self) -> None:
        with pytest.raises(SystemExit) as caught:
            call_command("shipped_seed", "delete-loop", "review", stdout=io.StringIO(), stderr=io.StringIO())

        assert caught.value.code == 1
        assert Loop.objects.filter(name="review").exists()

    def test_deleting_a_shipped_loop_with_the_phrase_succeeds(self) -> None:
        call_command(
            "shipped_seed",
            "delete-loop",
            "review",
            confirm=shipped_delete_phrase("review"),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )

        assert not Loop.objects.filter(name="review").exists()

    def test_deleting_a_shipped_schedule_needs_the_phrase(self) -> None:
        with pytest.raises(SystemExit):
            call_command("shipped_seed", "delete-schedule", "always-away", stdout=io.StringIO(), stderr=io.StringIO())

        assert ModeSchedule.objects.filter(name="always-away").exists()

    def test_deleting_a_shipped_preset_needs_the_phrase(self) -> None:
        with pytest.raises(SystemExit):
            call_command("shipped_seed", "delete-preset", "off", stdout=io.StringIO(), stderr=io.StringIO())

        assert Mode.objects.filter(name="off").exists()

    def test_a_blank_name_refuses(self) -> None:
        with pytest.raises(SystemExit) as caught:
            call_command("shipped_seed", "delete-loop", "", stdout=io.StringIO(), stderr=io.StringIO())

        assert caught.value.code == 1


class TestThePresetCliHonoursTheSharedSeam(django.test.TestCase):
    """`loop_preset delete` deleted the row directly, bypassing the referrer refusal."""

    def setUp(self) -> None:
        seed_default_presets_and_schedules()
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "standard")

    def test_a_preset_a_schedule_slot_names_is_refused(self) -> None:
        assert Mode.objects.filter(name="present").exists()

        with pytest.raises(SystemExit):
            call_command("loop_preset", "delete", "present", stdout=io.StringIO(), stderr=io.StringIO())

        assert Mode.objects.filter(name="present").exists(), "a referenced preset must survive"

    def test_a_shipped_unreferenced_preset_still_needs_the_phrase(self) -> None:
        with pytest.raises(SystemExit):
            call_command("loop_preset", "delete", "off", stdout=io.StringIO(), stderr=io.StringIO())

        assert Mode.objects.filter(name="off").exists()

    def test_the_phrase_lets_an_unreferenced_shipped_preset_go(self) -> None:
        call_command(
            "loop_preset",
            "delete",
            "off",
            confirm=shipped_delete_phrase("off"),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )

        assert not Mode.objects.filter(name="off").exists()


class TestTheDoctorCheck(django.test.TestCase):
    def setUp(self) -> None:
        seed_default_loops_and_prompts()
        seed_default_presets_and_schedules()
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "standard")
        _fleet_just_ticked()

    def test_it_fails_and_names_the_missing_row(self) -> None:
        Loop.objects.filter(name="review").delete()

        with mock.patch("typer.echo") as echo:
            ok = _check_shipped_seed_inertness()

        assert ok is False
        assert any("review" in str(call) for call in echo.call_args_list)

    def test_a_crash_in_the_reader_degrades_to_ok(self) -> None:
        with (
            mock.patch("teatree.loops.seed_inertness.shipped_inertness", side_effect=RuntimeError("db down")),
            mock.patch("typer.echo"),
        ):
            assert _check_shipped_seed_inertness() is True

    def test_deliberate_notes_alone_keep_the_check_green(self) -> None:
        with mock.patch("typer.echo"):
            assert _check_shipped_seed_inertness() is True
