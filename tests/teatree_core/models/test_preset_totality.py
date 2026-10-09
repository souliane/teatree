"""Every preset answers for every loop — repair at the seams, refusal in the admin (B1)."""

import pytest
from django.test import TestCase

from teatree.core.models import Loop, Mode
from teatree.core.models.preset_totality import PresetNotTotalError, require_total_entries, totalized_entries


def _loop(name: str) -> Loop:
    loop, _ = Loop.objects.update_or_create(
        name=name, defaults={"script": f"src/teatree/loops/{name}/loop.py", "delay_seconds": 60}
    )
    return loop


class TotalizedEntriesRepairs(TestCase):
    def setUp(self) -> None:
        Loop.objects.all().delete()
        _loop("inbox")
        _loop("ship")

    def test_a_loop_the_map_never_named_reads_off(self) -> None:
        assert totalized_entries({"inbox": True}) == {"inbox": True, "ship": False}

    def test_a_key_naming_no_live_loop_is_dropped(self) -> None:
        assert totalized_entries({"inbox": True, "retired_loop": True}) == {"inbox": True, "ship": False}

    def test_a_non_boolean_entry_reads_off_rather_than_truthy(self) -> None:
        assert totalized_entries({"inbox": "yes", "ship": 1}) == {"inbox": False, "ship": False}

    def test_a_map_that_is_not_a_map_totalizes_to_all_off(self) -> None:
        assert totalized_entries(None) == {"inbox": False, "ship": False}


class RequireTotalEntriesRefuses(TestCase):
    def setUp(self) -> None:
        Loop.objects.all().delete()
        _loop("inbox")
        _loop("ship")

    def test_a_total_map_passes_through_unchanged(self) -> None:
        assert require_total_entries({"inbox": True, "ship": False}, preset_name="present") == {
            "inbox": True,
            "ship": False,
        }

    def test_an_unnamed_loop_is_refused_by_name(self) -> None:
        with pytest.raises(PresetNotTotalError, match="no opinion on ship"):
            require_total_entries({"inbox": True}, preset_name="present")

    def test_a_stale_key_is_refused_by_name(self) -> None:
        with pytest.raises(PresetNotTotalError, match="names no live loop: gone"):
            require_total_entries({"inbox": True, "ship": False, "gone": False}, preset_name="present")

    def test_a_non_boolean_entry_is_refused_by_name(self) -> None:
        with pytest.raises(PresetNotTotalError, match="non-boolean entries: inbox"):
            require_total_entries({"inbox": "on", "ship": False}, preset_name="present")


class NewLoopsStartQuietEverywhere(TestCase):
    def setUp(self) -> None:
        Loop.objects.all().delete()
        Mode.objects.all().delete()
        _loop("inbox")

    def test_a_newborn_loop_is_written_off_into_every_preset(self) -> None:
        Mode.objects.create(name="present", entries={"inbox": True})
        Mode.objects.create(name="off", entries={"inbox": False})

        _loop("newcomer")

        assert Mode.objects.by_name("present").entries == {"inbox": True, "newcomer": False}
        assert Mode.objects.by_name("off").entries == {"inbox": False, "newcomer": False}

    def test_a_preset_that_already_holds_an_opinion_is_left_alone(self) -> None:
        Mode.objects.create(name="present", entries={"inbox": True, "newcomer": True})

        assert Mode.objects.backfill_loop("newcomer") == 0
        assert Mode.objects.by_name("present").entries["newcomer"] is True
