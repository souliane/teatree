"""Self-DB schema pre-flight on the sanctioned merge path (#869, #2006).

Before #869 `t3 teatree ticket clear`/`merge` called the ORM with no
schema check, so an unmigrated self-DB (missing `teatree_merge_clear`)
surfaced a raw `django.db.utils.OperationalError: no such table`
traceback. Since #863 prohibits raw `gh pr merge`, that opaque failure
blocked the entire merge pipeline.

#869 first converted that into a refusal with a remediation that asked
the operator to run `t3 teatree db migrate` by hand. #2006 closes the
loop: when the live editable install advances over a new migration the
self-DB falls behind, and the next sanctioned operation now self-heals
by applying the pending self-DB migrations in place (the same
non-destructive forward-only `migrate_self_db` the manual command runs)
before proceeding. A migrate that itself fails still fails LOUD with the
actionable remediation. These tests pin the self-heal behaviour and the
read-only doctor surfacing (the doctor reports the gap, it does not
heal).

Tests that exercise a function accepting an explicit ``alias`` argument
(``pending_migrations``, ``migrate_self_db``, ``require_current_schema``,
``doctor_check_self_db_migrations``) drive their stale/restore cycle
against a **private, file-backed SQLite connection** — never the shared,
xdist-worker-lifetime-reused ``default`` test database (#2915). The
handful of tests that exercise a sanctioned command (`ticket clear`,
`ticket merge`, `review record`) or the `t3 doctor check` aggregator
cannot move: those entry points hard-code ``DEFAULT_DB_ALIAS`` with no
alias parameter, so genuinely testing their self-heal behaviour requires
``default`` itself to be behind. Those stay on ``default``, scoped as
tightly as possible (see :class:`BehindSelfDbReportingTest` and
:class:`BehindSelfDbSelfHealsTest`).
"""

import io
from contextlib import redirect_stdout
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import OperationalError, ProgrammingError, connection
from django.db.utils import ConnectionDoesNotExist
from django.test import TransactionTestCase

from teatree.cli.doctor import run_doctor_checks as doctor_check
from teatree.core.gates.schema_guard import (
    SelfDbMigrationError,
    doctor_check_self_db_migrations,
    pending_migrations,
    require_current_schema,
)
from teatree.core.models import MergeClear
from tests.teatree_core._migration_graph import core_head_migration, core_migration_names
from tests.teatree_core.conftest import SchemaGuardAlias


def _unmigrate_core_to_zero() -> None:
    """Drive the self-DB genuinely behind via a real backward `migrate` (#2006).

    Migrating ``core`` back to ``zero`` reverses the whole chain: it drops
    every core table and clears the ``django_migrations`` ledger — the way
    the editable install *advances past* an unapplied migration. A forward
    heal re-applies the whole graph cleanly (re-creating the schema and
    re-running its seed/backfill data migrations) — exactly the state a
    keystone merge of a migration-adding PR leaves behind.
    """
    call_command("migrate", "core", "zero", "--no-input", verbosity=0)


def _migrate_core_forward() -> None:
    call_command("migrate", "core", "--no-input", verbosity=0)


def _unapply_initial_migration() -> None:
    """Reproduce the #869 self-DB symptom against the core graph, minimally.

    Drop the ``teatree_merge_clear`` table and un-record EVERY ``core`` migration
    from the ledger so the table is absent *and* the ``django_migrations`` ledger
    has no record of the migrations — precisely what ``MigrationExecutor``
    inspects to report a pending migration. Un-recording only ``0001_initial``
    would not surface a pending migration once a later leaf (``0002…``) is still
    recorded applied: ``migration_plan`` to that recorded leaf is empty. Clearing
    the whole core ledger makes the plan report the unapplied chain (``0001_initial``
    first). The other core tables are left in place: this is the narrowest
    mutation of the shared ``default`` connection that still reproduces the
    symptom (#2915) — the one remaining test here (`doctor_check()`, the `t3
    doctor check` aggregator) hard-codes ``DEFAULT_DB_ALIAS`` with no alias
    parameter, so it cannot move to a private connection like its siblings did.
    """
    with connection.schema_editor() as editor:
        editor.delete_model(MergeClear)
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM django_migrations WHERE app = 'core'")


def _reapply_initial_migration() -> None:
    """Restore the table + ledger so ``TransactionTestCase`` teardown flushes.

    Re-records every on-disk ``core`` migration the symptom-reproduction cleared
    (not just ``0001_initial``) so ``migration_plan`` sees the graph current
    again. Idempotent: a #2006 self-heal may already have re-created the table and
    re-recorded the ledger rows before this cleanup runs, so it tolerates an
    already-current schema rather than colliding on a duplicate table/row.
    """
    if not _table_exists(MergeClear._meta.db_table):
        with connection.schema_editor() as editor:
            editor.create_model(MergeClear)
    with connection.cursor() as cursor:
        for name in core_migration_names():
            cursor.execute(
                "INSERT OR IGNORE INTO django_migrations (app, name, applied) VALUES ('core', %s, CURRENT_TIMESTAMP)",
                [name],
            )


def _table_exists(table: str) -> bool:
    return table in connection.introspection.table_names()


class PendingMigrationsTest(TransactionTestCase):
    def test_returns_empty_when_schema_current(self) -> None:
        assert pending_migrations() == []

    def test_require_current_schema_is_noop_when_current(self) -> None:
        require_current_schema()  # must not raise


# Every test here calls ``make_stale()`` — a full multi-app migrate plus a
# reverse-migrate of ``core`` to ``zero`` on a fresh sqlite alias (several
# seconds single-core). Under maximum ``-n auto --cov --doctest-modules``
# parallel contention that exceeds the global 60s ``pytest-timeout``, so the
# genuinely-slow migrations get a scoped 240s bump; the global timeout stays
# 60s as the hang-detector for all other tests (#1189).
@pytest.mark.timeout(240)
class TestSchemaGuardOnPrivateAlias:
    """Read-only / self-heal surfaces exercised against a private alias (#2915).

    Every function under test here (``pending_migrations``, ``require_current_
    schema``, ``doctor_check_self_db_migrations``) accepts an explicit
    ``alias`` argument, so each test drives its own throwaway, file-backed
    SQLite connection (via the shared ``schema_guard_alias`` fixture)
    reverse-migrated to ``zero`` — never the shared ``default`` connection
    every other test in the xdist worker reuses. A crashed reverse-migrate/
    restore cycle here can corrupt only the one file the fixture created,
    which it tears down itself.
    """

    def test_raw_orm_call_would_fail_with_operationalerror(self, schema_guard_alias: SchemaGuardAlias) -> None:
        alias = schema_guard_alias.make_stale()
        # Anchor: before any heal the ORM raises the raw, opaque error this
        # whole module exists to replace.
        with pytest.raises(OperationalError, match="no such table"):
            MergeClear.objects.using(alias).count()

    def test_doctor_surface_fails_and_names_pending_migrations(self, schema_guard_alias: SchemaGuardAlias) -> None:
        alias = schema_guard_alias.make_stale()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = doctor_check_self_db_migrations(alias)
        assert result is False
        out = buffer.getvalue()
        assert "unapplied migration" in out
        # A fresh/zero DB applies the current head (the squash replaces the range it
        # collapses), so the doctor names ``core.<head>`` — derived from disk so a
        # future squash/leaf never re-breaks this.
        assert core_head_migration() in out

    def test_migrate_failure_fails_loud_with_remediation(self, schema_guard_alias: SchemaGuardAlias) -> None:
        alias = schema_guard_alias.make_stale()
        # When the heal itself fails, the gate must fail LOUD with the
        # actionable remediation — never silently swallow a half-migrated DB.
        with (
            patch(
                "teatree.core.gates.schema_guard.migrate_self_db",
                side_effect=SelfDbMigrationError("migrate exploded mid-apply"),
            ),
            pytest.raises(SelfDbMigrationError) as exc,
        ):
            require_current_schema(alias)
        message = str(exc.value)
        assert "unapplied migration" in message
        assert "t3 teatree db migrate" in message
        assert "migrate exploded mid-apply" in message

    def test_require_current_schema_auto_applies_then_proceeds(self, schema_guard_alias: SchemaGuardAlias) -> None:
        alias = schema_guard_alias.make_stale()
        assert pending_migrations(alias), "guard precondition: the self-DB must be behind for this test"
        with pytest.raises(OperationalError, match="no such table"):
            MergeClear.objects.using(alias).count()  # behind: the table genuinely does not exist

        require_current_schema(alias)  # heals in place — must not raise

        assert pending_migrations(alias) == [], "the pending migrations should have been applied"
        assert MergeClear.objects.using(alias).count() == 0  # healed: the table is now usable


class BehindSelfDbReportingTest(TransactionTestCase):
    """The one surface that cannot move off ``default`` (#2915).

    ``t3 doctor check`` (the aggregator, not the schema-guard-specific
    ``doctor_check_self_db_migrations`` it wraps) has no ``alias`` parameter —
    it always inspects ``DEFAULT_DB_ALIAS`` — so genuinely exercising it needs
    ``default`` itself to be behind. The mutation is the narrowest available
    (one table dropped, one app's ledger rows cleared, see
    ``_unapply_initial_migration``) rather than a full reverse-migrate, to
    keep the shared connection's exposure as small as possible.
    """

    def setUp(self) -> None:
        _unapply_initial_migration()
        self.addCleanup(_reapply_initial_migration)

    def test_doctor_check_command_aggregates_pending_migrations(self) -> None:
        # The `t3 doctor check` aggregation wires the schema-guard surface
        # into its pass/fail result, so the gap shows at session start.
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            ok = doctor_check()
        assert ok is False
        assert "unapplied migration" in buffer.getvalue()


# Each test drives a real backward ``migrate core zero`` in ``setUp`` and a full
# forward heal, on the shared ``default`` connection — several seconds
# single-core that exceeds the global 60s ``pytest-timeout`` under maximum
# parallel contention. Scoped 240s bump for the genuinely-slow migrations; the
# global 60s stays as the hang-detector everywhere else (#1189).
@pytest.mark.timeout(240)
class BehindSelfDbSelfHealsTest(TransactionTestCase):
    """The sanctioned commands that cannot move off ``default`` (#2915).

    ``ticket clear``, ``ticket merge`` and ``review record`` call
    ``require_current_schema()`` with no ``alias`` argument — they always
    self-heal ``DEFAULT_DB_ALIAS``. Testing their self-heal integration for
    real (a keystone merge advanced the install over a migration, #2006)
    needs ``default`` itself genuinely behind, via a real backward `migrate
    core zero` (every core table dropped, ledger cleared) — the state a real
    ``migrate`` can heal, unlike the cheap symptom reproduction above.
    """

    def setUp(self) -> None:
        _unmigrate_core_to_zero()
        self.addCleanup(_migrate_core_forward)

    def test_ticket_clear_command_proceeds_past_schema_gate(self) -> None:
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                866,
                "statusline-stale-wakeup",
                reviewed_sha="29f0a77a4fd03bd281b23e53cfc47ea9a928620b",
                reviewer_identity="coldrev-866",
                blast_class="logic",
            ),
        )
        # The clear proceeds past the schema gate; whatever its outcome, it
        # is never the unapplied-migration refusal that #2006 eliminates.
        assert "unapplied migration" not in str(result.get("error", ""))
        assert pending_migrations() == []

    def test_ticket_merge_command_proceeds_past_schema_gate(self) -> None:
        result = cast(
            "dict[str, object]",
            call_command("ticket", "merge", 1),
        )
        assert "unapplied migration" not in str(result.get("error", ""))
        assert pending_migrations() == []

    def test_review_record_command_proceeds_past_schema_gate(self) -> None:
        result = cast(
            "dict[str, object]",
            call_command(
                "review",
                "record",
                866,
                "souliane/teatree",
                reviewed_sha="29f0a77a4fd03bd281b23e53cfc47ea9a928620b",
                reviewer_identity="coldrev-866",
            ),
        )
        assert "unapplied migration" not in str(result.get("error", ""))
        assert pending_migrations() == []


class DoctorCheckCurrentSchemaTest(TransactionTestCase):
    def test_doctor_check_passes_when_schema_current(self) -> None:
        assert doctor_check_self_db_migrations() is True

    def test_doctor_check_warns_but_does_not_fail_when_db_unreachable(self) -> None:
        # DB absent/offline at session start is a valid state — the doctor
        # check must WARN, not crash or block the session with a FAIL.
        buffer = io.StringIO()
        with (
            patch(
                "teatree.core.gates.schema_guard.pending_migrations",
                side_effect=OperationalError("unable to open database file"),
            ),
            redirect_stdout(buffer),
        ):
            result = doctor_check_self_db_migrations()
        assert result is True
        assert "Could not inspect self-DB migrations" in buffer.getvalue()

    def test_doctor_check_fails_on_misconfigured_connection(self) -> None:
        # A wrong alias raises ConnectionDoesNotExist (not a DatabaseError) —
        # a real misconfiguration, not a legit DB-absent state. The check
        # must fail CLOSED (FAIL/False), never report the schema current by
        # swallowing the error (#1987 fail-open).
        buffer = io.StringIO()
        with (
            patch(
                "teatree.core.gates.schema_guard.pending_migrations",
                side_effect=ConnectionDoesNotExist("The connection 'bogus' doesn't exist."),
            ),
            redirect_stdout(buffer),
        ):
            result = doctor_check_self_db_migrations()
        assert result is False
        assert "FAIL" in buffer.getvalue()

    def test_doctor_check_fails_on_orm_programming_error(self) -> None:
        # A ProgrammingError (ORM regression / bad query) is a real defect,
        # not a benign DB-absent state — the check must fail CLOSED.
        buffer = io.StringIO()
        with (
            patch(
                "teatree.core.gates.schema_guard.pending_migrations",
                side_effect=ProgrammingError("relation does not exist"),
            ),
            redirect_stdout(buffer),
        ):
            result = doctor_check_self_db_migrations()
        assert result is False
        assert "FAIL" in buffer.getvalue()
