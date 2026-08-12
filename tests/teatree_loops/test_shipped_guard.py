"""Deleting a SHIPPED loop/preset/schedule needs a typed phrase naming what stops (#3842).

Soft-protection, not prohibition. A shipped row deletes on `t3 setup`'s own terms (the seed
is ``get_or_create`` by name, so a removed row comes back), which makes deletion the
*recoverable* failure — forbidding it outright addresses a case that has never occurred
while inviting the hand-edited DB, which is strictly worse than an audited delete.

The phrase names the consequence rather than repeating a generic DELETE, because the
operator deleting a shipped loop is usually unclear on what it does: a refusal that quotes
the shipped description teaches more than one that just says no.
"""

import django.test
import pytest

from teatree.core.models import ConfigSetting, Loop, Mode, ModeSchedule, ModeScheduleSlot
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING
from teatree.loops.loop_admin import delete_loop
from teatree.loops.preset_admin import delete_preset
from teatree.loops.preset_editing import PresetEditError
from teatree.loops.preset_seed import seed_default_presets_and_schedules
from teatree.loops.schedule_editing import delete_schedule
from teatree.loops.seed import seed_default_loops_and_prompts
from teatree.loops.shipped_guard import is_shipped, shipped_delete_phrase


class TestAShippedLoopNeedsThePhrase(django.test.TestCase):
    def setUp(self) -> None:
        seed_default_loops_and_prompts()

    def test_no_confirm_refuses_and_the_row_survives(self) -> None:
        with pytest.raises(PresetEditError):
            delete_loop("review")

        assert Loop.objects.filter(name="review").exists(), "a refused delete must not have deleted anything"

    def test_the_wrong_phrase_refuses(self) -> None:
        with pytest.raises(PresetEditError):
            delete_loop("review", confirm="yes")

        assert Loop.objects.filter(name="review").exists()

    def test_the_exact_phrase_deletes(self) -> None:
        delete_loop("review", confirm=shipped_delete_phrase("review"))

        assert not Loop.objects.filter(name="review").exists()

    def test_the_refusal_quotes_the_shipped_description_and_the_phrase(self) -> None:
        shipped = Loop.objects.get(name="review").description

        with pytest.raises(PresetEditError) as caught:
            delete_loop("review")

        message = str(caught.value)
        assert shipped[:40] in message, "the refusal must name what stops happening"
        assert "stop-review" in message, "the refusal must name the phrase to type"
        assert "t3 setup" in message, "deletion is recoverable — say so"

    def test_an_operator_created_loop_needs_no_phrase(self) -> None:
        Loop.objects.create(name="operator-custom", script="src/teatree/loops/x/loop.py", delay_seconds=300)

        delete_loop("operator-custom")

        assert not Loop.objects.filter(name="operator-custom").exists()

    def test_deleting_a_name_that_does_not_exist_refuses(self) -> None:
        with pytest.raises(PresetEditError):
            delete_loop("no-such-loop")


class TestAShippedPresetNeedsThePhrase(django.test.TestCase):
    def setUp(self) -> None:
        seed_default_presets_and_schedules()

    def test_no_confirm_refuses_and_the_row_survives(self) -> None:
        with pytest.raises(PresetEditError):
            delete_preset("maintenance")

        assert Mode.objects.filter(name="maintenance").exists()

    def test_the_exact_phrase_deletes(self) -> None:
        delete_preset("maintenance", confirm=shipped_delete_phrase("maintenance"))

        assert not Mode.objects.filter(name="maintenance").exists()

    def test_a_live_referrer_still_refuses_even_with_the_phrase(self) -> None:
        """The confirm is a speed bump on a safe delete — never an override of an integrity refusal."""
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "standard")

        with pytest.raises(PresetEditError) as caught:
            delete_preset("present", confirm=shipped_delete_phrase("present"))

        assert "cannot delete" in str(caught.value)
        assert Mode.objects.filter(name="present").exists()


class TestAShippedScheduleNeedsThePhrase(django.test.TestCase):
    def setUp(self) -> None:
        seed_default_presets_and_schedules()
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "standard")

    def test_no_confirm_refuses_and_the_row_survives(self) -> None:
        with pytest.raises(PresetEditError):
            delete_schedule("always-away")

        assert ModeSchedule.objects.filter(name="always-away").exists()

    def test_the_exact_phrase_deletes_it_and_its_slots(self) -> None:
        delete_schedule("always-away", confirm=shipped_delete_phrase("always-away"))

        assert not ModeSchedule.objects.filter(name="always-away").exists()
        assert not ModeScheduleSlot.objects.filter(schedule__name="always-away").exists()

    def test_the_active_calendar_refuses_even_with_the_phrase(self) -> None:
        with pytest.raises(PresetEditError) as caught:
            delete_schedule("standard", confirm=shipped_delete_phrase("standard"))

        assert "active" in str(caught.value)
        assert ModeSchedule.objects.filter(name="standard").exists()


class TestWhatCountsAsShipped(django.test.TestCase):
    def test_every_family_recognises_its_own_shipped_names(self) -> None:
        assert is_shipped("loop", "review")
        assert is_shipped("preset", "present")
        assert is_shipped("schedule", "standard")

    def test_a_name_that_ships_in_another_family_is_not_shipped_here(self) -> None:
        assert not is_shipped("preset", "review"), "families must not leak into each other"
        assert not is_shipped("loop", "present")

    def test_an_operator_created_name_is_not_shipped(self) -> None:
        assert not is_shipped("loop", "operator-custom")

    def test_the_phrase_names_the_thing_that_stops(self) -> None:
        assert shipped_delete_phrase("review") == "stop-review"
