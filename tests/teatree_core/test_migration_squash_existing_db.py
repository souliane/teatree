"""An existing DB carried across the core migration squash: which ``django_migrations`` rows it may keep.

The core history was squashed into ONE initial migration under a NEW name (never a pre-squash name),
so it is unapplied on an install that ran the pre-squash chain. The squash's live window
therefore rewrites that install's core rows in one transaction: every pre-squash core row
is deleted and the squash is recorded as applied (fake-applied: the schema is already there).

Each half is load-bearing, and these tests pin both.

A leftover pre-squash row is not harmless: :mod:`teatree.core.process_freshness` reads the
NEWEST core row by id against the loaded ``max_migration.txt``, so a leftover ``0127_*`` row
reads as a schema newer than the loaded squash: BEHIND, every claim refused.

The squash's own row is what stops a migrate from re-creating every table. Without it,
``migrate`` re-runs its ``CreateModel`` operations against tables that exist and bricks.

The new name is the rollback guard: pre-squash code meeting a cleaned DB finds none of its own
rows (its ``0001_initial`` included), so its first migrate step fails on the first existing
table, inside that migration's transaction, instead of layering ``0002``.. over the squashed
schema. The squash name must therefore never be a pre-squash migration name.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.db.utils import OperationalError
from django.test import TransactionTestCase

from teatree.core.process_freshness import FreshnessVerdict, LoadedSnapshot, _compare
from tests.teatree_core._migration_graph import core_head_migration, core_initial_migration, core_migration_names

#: Pre-squash core migration names an un-cleaned install carries (frozen history, never on disk again).
_PRE_SQUASH_INITIAL = "0001_initial"
_PRE_SQUASH_LEAF = "0127_remove_session_repo_ledgers"
_PRE_SQUASH_ROWS = (
    _PRE_SQUASH_INITIAL,
    "0001_squashed_0030",
    "0126_name_the_worker_generation_and_cursor_tables",
    _PRE_SQUASH_LEAF,
)


def test_core_is_one_initial_migration_under_a_new_name() -> None:
    disk = MigrationLoader(None).disk_migrations  # disk only, no DB
    initials = [name for name in core_migration_names() if disk["core", name].initial]
    assert initials == [core_initial_migration()]
    assert not set(core_migration_names()) & set(_PRE_SQUASH_ROWS)


def test_agents_md_prescribes_a_new_name_and_no_automatic_rollback_for_the_next_squash() -> None:
    agents = (Path(__file__).resolve().parents[2] / "AGENTS.md").read_text(encoding="utf-8")
    _, found, rest = agents.partition("\n### Squashing the core migrations")
    assert found, "AGENTS.md has no 'Squashing the core migrations' section"
    section = " ".join(rest.split("\n## ", 1)[0].split("\n### ", 1)[0].split())
    for clause in ("NEW name", "`0001_initial`", "no automatic rollback", "pre-squash backup", "`_PRE_SQUASH_ROWS`"):
        assert clause in section, f"the squash section no longer says {clause!r}"


@pytest.mark.timeout(480)
class TestLeftoverPreSquashRows(TransactionTestCase):
    def setUp(self) -> None:
        self.recorder = MigrationRecorder(connection)
        self.addCleanup(self._drop_leftovers)

    def _drop_leftovers(self) -> None:
        self.recorder.migration_qs.filter(app="core").exclude(name__in=core_migration_names()).delete()

    def _freshness(self) -> FreshnessVerdict:
        snapshot = LoadedSnapshot(heads={"core": core_head_migration()}, started_at=datetime.now(UTC))
        return _compare(snapshot, connection.alias).verdict

    def test_fileless_pre_squash_rows_plan_nothing(self) -> None:
        for name in _PRE_SQUASH_ROWS:
            self.recorder.record_applied("core", name)
        executor = MigrationExecutor(connection)
        assert executor.migration_plan(executor.loader.graph.leaf_nodes()) == []

    def test_a_leftover_pre_squash_row_reads_behind(self) -> None:
        self.recorder.record_applied("core", _PRE_SQUASH_LEAF)
        assert self._freshness() is FreshnessVerdict.BEHIND

    def test_once_the_leftovers_are_deleted_it_reads_current(self) -> None:
        self.recorder.record_applied("core", _PRE_SQUASH_LEAF)
        self._drop_leftovers()
        assert self._freshness() is FreshnessVerdict.CURRENT


@pytest.mark.timeout(480)
class TestTheSquashRowIsLoadBearing(TransactionTestCase):
    def test_without_the_squash_row_migrate_bricks_on_the_existing_tables(self) -> None:
        recorder = MigrationRecorder(connection)
        for name in core_migration_names():
            recorder.record_unapplied("core", name)
            self.addCleanup(recorder.record_applied, "core", name)
        with pytest.raises(OperationalError, match="already exists"):
            call_command("migrate", "core", "--no-input", verbosity=0)
