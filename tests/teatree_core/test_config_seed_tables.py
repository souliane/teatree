"""The seed half of the TOML interchange — ``[loops]`` / ``[modes]`` / ``[schedules]``.

Seed rows ride the SAME override rule a ``ConfigSetting`` row does: only a field an operator
tuned AWAY from its ``defaults.toml`` seed is exported, so an untouched box exports no seed
table at all. That rule is what keeps "a dump of ``defaults.toml`` imports to zero rows"
true of the seed tables and not only of the settings table.

The write side is deliberately narrow: an import RESTORES tuning onto rows the install seed
already made, and never conjures a loop teatree does not ship.
"""

import tomllib

import pytest
import tomlkit
from django.test import TestCase
from tomlkit import items as tomlkit_items

from teatree.core.config_seed_tables import (
    SeedFieldDisposition,
    classify_seed_rows,
    emit_seed_tables,
    live_seed_rows,
    unseeded_entries,
    write_seed_field,
)
from teatree.core.models import Loop
from teatree.core.models.config_setting import ConfigValue

_SEEDED_LOOP = "housekeeping"


def _sorted_table(rows: dict[str, ConfigValue]) -> tomlkit_items.Table:
    table = tomlkit.table()
    for key in sorted(rows):
        table[key] = rows[key]
    return table


def _emitted() -> dict[str, dict[str, dict[str, ConfigValue]]]:
    """The emitted seed tables, read back through a real TOML parse."""
    document = tomlkit.document()
    emit_seed_tables(document, _sorted_table)
    return tomllib.loads(tomlkit.dumps(document))


class TestLiveSeedRows(TestCase):
    def test_each_row_reports_its_seed_fields_under_its_name(self) -> None:
        loop = Loop.objects.get(name=_SEEDED_LOOP)
        assert live_seed_rows("loops")[_SEEDED_LOOP]["delay_seconds"] == loop.delay_seconds


class TestEmitOnlyDivergences(TestCase):
    """The override rule: an untouched box exports no seed table."""

    def test_an_untouched_box_emits_nothing(self) -> None:
        assert _emitted() == {}

    def test_a_tuned_field_is_emitted_under_its_family_and_name(self) -> None:
        Loop.objects.filter(name=_SEEDED_LOOP).update(delay_seconds=4242)
        assert _emitted()["loops"][_SEEDED_LOOP] == {"delay_seconds": 4242}

    def test_the_untuned_fields_of_a_tuned_row_stay_out(self) -> None:
        Loop.objects.filter(name=_SEEDED_LOOP).update(delay_seconds=4242)
        assert set(_emitted()["loops"][_SEEDED_LOOP]) == {"delay_seconds"}


class TestClassifySeedRows(TestCase):
    def test_a_value_equal_to_the_shipped_seed_is_skipped_not_written(self) -> None:
        shipped = live_seed_rows("loops")[_SEEDED_LOOP]["delay_seconds"]
        kinds = {d.kind for d in classify_seed_rows({"loops": {_SEEDED_LOOP: {"delay_seconds": shipped}}})}
        assert kinds == {"skip"}

    def test_a_diverging_value_is_a_write(self) -> None:
        [disposition] = classify_seed_rows({"loops": {_SEEDED_LOOP: {"delay_seconds": 4242}}})
        assert (disposition.kind, disposition.value) == ("write", 4242)

    def test_an_unknown_entry_is_rejected_with_a_reason(self) -> None:
        [disposition] = classify_seed_rows({"loops": {"no-such-loop": {"delay_seconds": 60}}})
        assert disposition.kind == "reject"
        assert disposition.reason

    def test_an_unknown_field_is_rejected(self) -> None:
        [disposition] = classify_seed_rows({"loops": {_SEEDED_LOOP: {"not_a_field": 1}}})
        assert disposition.kind == "reject"

    def test_a_non_table_entry_is_ignored_rather_than_crashing(self) -> None:
        assert classify_seed_rows({"loops": {_SEEDED_LOOP: "not-a-table"}}) == []

    def test_the_scope_label_names_the_family_and_the_entry(self) -> None:
        disposition = SeedFieldDisposition("loops", _SEEDED_LOOP, "delay_seconds", 60, "write", "")
        assert disposition.scope == f"loops.{_SEEDED_LOOP}"


class TestUnseededEntries(TestCase):
    def test_a_write_whose_row_exists_is_not_reported(self) -> None:
        writes = [SeedFieldDisposition("loops", _SEEDED_LOOP, "delay_seconds", 60, "write", "")]
        assert unseeded_entries(writes) == set()

    def test_a_write_whose_row_was_never_seeded_is_reported(self) -> None:
        # The box that never ran the install seed: the caller refuses the whole import
        # rather than letting the write raise mid-run.
        Loop.objects.filter(name=_SEEDED_LOOP).delete()
        writes = [SeedFieldDisposition("loops", _SEEDED_LOOP, "delay_seconds", 60, "write", "")]
        assert unseeded_entries(writes) == {("loops", _SEEDED_LOOP)}


class TestWriteSeedField(TestCase):
    def test_the_field_lands_on_the_row_it_names(self) -> None:
        write_seed_field("loops", _SEEDED_LOOP, "delay_seconds", 4242)
        assert Loop.objects.get(name=_SEEDED_LOOP).delay_seconds == 4242

    def test_it_never_creates_a_row(self) -> None:
        before = Loop.objects.count()
        with pytest.raises(Loop.DoesNotExist):
            write_seed_field("loops", "no-such-loop", "delay_seconds", 60)
        assert Loop.objects.count() == before
