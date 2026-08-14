"""The export's SCOPE statement — the pin that keeps the stated breadth and the file together.

The interchange reaches past the settings store into the loop, preset and schedule rows, and
the page hosting it says so (#4340). A statement is only worth reading if it cannot go stale,
so what the export EMITS is asserted against what the statement NAMES: a new top-level table
that nothing documents reds this file rather than shipping a page that quietly under-states
what an operator is about to apply.
"""

import tomllib

from django.test import TestCase

from teatree.config.seed_defaults import SEED_TABLES
from teatree.core.config_interchange.document_layout import E2E_REPOS_TABLE, OVERLAYS_TABLE, TEATREE_TABLE
from teatree.core.config_interchange.migration import export_db_to_toml
from teatree.core.config_interchange.scope import EXPORT_SECTIONS, section_for_row
from teatree.core.config_interchange.secret_guard import PRIVATE_BACKUP_TABLE
from teatree.core.models import ConfigSetting, Loop, Mode, ModeSchedule

_DOCUMENTED = frozenset(section.table for section in EXPORT_SECTIONS)


def _diverge_every_family() -> None:
    """Tune one row of every family, so the export emits every table it is able to."""
    ConfigSetting.objects.set_value("mode", "auto")
    ConfigSetting.objects.set_value("merge_wip", 7, scope="demo-overlay")
    ConfigSetting.objects.set_value("e2e_repos", {"demo": {"url": "https://example.invalid/demo.git"}})
    Loop.objects.filter(name="housekeeping").update(delay_seconds=4242)
    Mode.objects.update_or_create(name="present", defaults={"description": "tuned"})
    ModeSchedule.objects.update_or_create(name="standard", defaults={"description": "tuned"})


def _emitted_tables(*, include_private: bool = False) -> set[str]:
    dump = export_db_to_toml(include_private=include_private, scan_terms=()).toml
    return set(tomllib.loads(dump))


class TestTheStatementNamesEveryTableTheExportEmits(TestCase):
    def test_a_box_tuned_in_every_family_emits_only_documented_tables(self) -> None:
        _diverge_every_family()
        assert _emitted_tables() <= _DOCUMENTED

    def test_a_box_tuned_in_every_family_emits_them_all(self) -> None:
        _diverge_every_family()
        assert _emitted_tables() == _DOCUMENTED

    def test_the_control_would_catch_a_table_nothing_documents(self) -> None:
        # Anti-vacuous: the assertion above passes on an empty document too, so prove the
        # subset test can actually fail before trusting it.
        assert not {"a_table_nothing_documents"} <= _DOCUMENTED

    def test_a_private_backup_adds_only_its_own_format_marker(self) -> None:
        # `[backup]` declares the FILE's format rather than carrying config, and the page
        # never emits it — so it is the one table outside the statement, named here.
        _diverge_every_family()
        assert _emitted_tables(include_private=True) - {PRIVATE_BACKUP_TABLE} <= _DOCUMENTED

    def test_the_statement_is_derived_from_the_layout_the_writers_use(self) -> None:
        assert {TEATREE_TABLE, OVERLAYS_TABLE, E2E_REPOS_TABLE, *SEED_TABLES} == _DOCUMENTED

    def test_every_section_says_what_it_covers(self) -> None:
        for section in EXPORT_SECTIONS:
            assert section.label, section.table
            assert section.covers, section.table


class TestSectionForRow(TestCase):
    """An import report's rows, canonicalised UP to the table each came out of."""

    def test_a_global_setting_row_belongs_to_the_settings_table(self) -> None:
        assert section_for_row("", "mode") == TEATREE_TABLE

    def test_a_scoped_setting_row_belongs_to_the_overlays_table(self) -> None:
        assert section_for_row("demo-overlay", "merge_wip") == OVERLAYS_TABLE

    def test_each_registry_row_belongs_to_its_own_table(self) -> None:
        assert section_for_row("", OVERLAYS_TABLE) == OVERLAYS_TABLE
        assert section_for_row("", E2E_REPOS_TABLE) == E2E_REPOS_TABLE

    def test_a_seed_row_belongs_to_its_own_family(self) -> None:
        assert section_for_row("loops.dream", "default_enabled") == "loops"
        assert section_for_row("schedules.standard", "timezone") == "schedules"

    def test_an_overlay_sharing_a_family_name_is_still_an_overlay(self) -> None:
        # The dot is what makes a scope a seed row; a bare name is an overlay scope.
        assert section_for_row("loops", "merge_wip") == OVERLAYS_TABLE
