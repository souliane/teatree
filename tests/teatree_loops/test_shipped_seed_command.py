"""``manage.py shipped_seed`` renders the audit and EXITS NON-ZERO on a fault (#3842).

The exit code is the load-bearing part and the easiest thing to get silently wrong: a
``typer.Exit`` raised inside a ``TyperCommand`` reached through ``call_command`` is
swallowed, so the process exits 0 and CI reports green on a real failure. These tests
assert ``SystemExit`` with its code, which is the only shape that survives the call chain.
"""

import io
import json
from unittest import mock

import django.test
import pytest
from django.core.management import call_command

from teatree.cli.doctor.app import _check_shipped_seed_inertness
from teatree.core.models import ConfigSetting, Loop, Mode, ModeSchedule
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING
from teatree.loops.preset_seed import seed_default_presets_and_schedules
from teatree.loops.seed import seed_default_loops_and_prompts
from teatree.loops.shipped_guard import shipped_delete_phrase


class TestAuditExitCode(django.test.TestCase):
    def setUp(self) -> None:
        seed_default_loops_and_prompts()
        seed_default_presets_and_schedules()
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "standard")

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
        """`always-unattended` is inactive by design — a note, never a non-zero exit."""
        err = io.StringIO()

        call_command("shipped_seed", "audit", stdout=io.StringIO(), stderr=err)

        assert "always-unattended" in err.getvalue(), "the note is still reported"


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
            call_command(
                "shipped_seed", "delete-schedule", "always-unattended", stdout=io.StringIO(), stderr=io.StringIO()
            )

        assert ModeSchedule.objects.filter(name="always-unattended").exists()

    def test_deleting_a_shipped_preset_needs_the_phrase(self) -> None:
        with pytest.raises(SystemExit):
            call_command("shipped_seed", "delete-preset", "heads-down", stdout=io.StringIO(), stderr=io.StringIO())

        assert Mode.objects.filter(name="heads-down").exists()

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
        assert Mode.objects.filter(name="engaged").exists()

        with pytest.raises(SystemExit):
            call_command("loop_preset", "delete", "engaged", stdout=io.StringIO(), stderr=io.StringIO())

        assert Mode.objects.filter(name="engaged").exists(), "a referenced preset must survive"

    def test_a_shipped_unreferenced_preset_still_needs_the_phrase(self) -> None:
        with pytest.raises(SystemExit):
            call_command("loop_preset", "delete", "heads-down", stdout=io.StringIO(), stderr=io.StringIO())

        assert Mode.objects.filter(name="heads-down").exists()

    def test_the_phrase_lets_an_unreferenced_shipped_preset_go(self) -> None:
        call_command(
            "loop_preset",
            "delete",
            "heads-down",
            confirm=shipped_delete_phrase("heads-down"),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )

        assert not Mode.objects.filter(name="heads-down").exists()


class TestTheDoctorCheck(django.test.TestCase):
    def setUp(self) -> None:
        seed_default_loops_and_prompts()
        seed_default_presets_and_schedules()
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "standard")

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
