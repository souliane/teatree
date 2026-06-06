"""Self-DB schema pre-flight on the sanctioned merge path (#869).

Before #869 `t3 teatree ticket clear`/`merge` called the ORM with no
schema check, so an unmigrated self-DB (missing `teatree_merge_clear`)
surfaced a raw `django.db.utils.OperationalError: no such table`
traceback. Since #863 prohibits raw `gh pr merge`, that opaque failure
blocked the entire merge pipeline. These tests pin the actionable
behaviour and the doctor surfacing.
"""

import io
from contextlib import redirect_stdout
from typing import ClassVar, cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import OperationalError, ProgrammingError, connection
from django.db.utils import ConnectionDoesNotExist
from django.test import TransactionTestCase

from teatree.cli.doctor import check as doctor_check
from teatree.core.models import MergeClear
from teatree.core.schema_guard import (
    SelfDbMigrationError,
    doctor_check_self_db_migrations,
    pending_migrations,
    require_current_schema,
)

_MERGE_MIGRATIONS = ("0011_mergeclear_mergeaudit", "0012_mergeclear_human_authorizer")


class _UnapplyState:
    """Carries the un-recorded migration tail between setup and cleanup."""

    tail: ClassVar[list[str]] = []


def _core_migrations_after_merge() -> list[str]:
    """Ledger names for every applied core migration newer than 0012.

    Django's ``MigrationExecutor`` treats a dependency as satisfied when a
    *descendant* is recorded as applied. So un-recording only 0011/0012
    while a later migration (0013+) stays in the ledger masks the gap —
    the plan to the leaf is empty and the guard sees nothing pending.
    Reproducing the #869 state faithfully therefore means un-recording
    the whole contiguous tail from 0011 onward, not just the two merge
    migrations. Discovered from the ledger so a future migration cannot
    silently re-mask the gap again.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT name FROM django_migrations WHERE app = 'core' AND name > %s ORDER BY name",
            [_MERGE_MIGRATIONS[-1]],
        )
        return [row[0] for row in cursor.fetchall()]


def _unapply_merge_migrations() -> None:
    """Reproduce the real #869 self-DB state exactly.

    The dogfood DB ran #863/#864's code but never applied migrations
    0011/0012, so the ``teatree_merge_clear`` table is absent *and* the
    ``django_migrations`` ledger has no record of them — precisely what
    ``MigrationExecutor`` inspects.
    """
    _UnapplyState.tail = _core_migrations_after_merge()
    with connection.schema_editor() as editor:
        editor.delete_model(MergeClear)
    with connection.cursor() as cursor:
        cursor.executemany(
            "DELETE FROM django_migrations WHERE app = 'core' AND name = %s",
            [(name,) for name in (*_MERGE_MIGRATIONS, *_UnapplyState.tail)],
        )


def _reapply_merge_migrations() -> None:
    """Restore the table + ledger so ``TransactionTestCase`` teardown flushes."""
    with connection.schema_editor() as editor:
        editor.create_model(MergeClear)
    with connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO django_migrations (app, name, applied) VALUES ('core', %s, CURRENT_TIMESTAMP)",
            [(name,) for name in (*_MERGE_MIGRATIONS, *_UnapplyState.tail)],
        )


class PendingMigrationsTest(TransactionTestCase):
    def test_returns_empty_when_schema_current(self) -> None:
        assert pending_migrations() == []

    def test_require_current_schema_is_noop_when_current(self) -> None:
        require_current_schema()  # must not raise


class UnmigratedSelfDbTest(TransactionTestCase):
    """The exact #869 reproduction: the MergeClear table is absent."""

    def setUp(self) -> None:
        _unapply_merge_migrations()
        self.addCleanup(_reapply_merge_migrations)

    def test_raw_orm_call_would_fail_with_operationalerror(self) -> None:
        # Anti-vacuous anchor: without the guard the ORM raises the raw,
        # opaque error this fix exists to replace.
        with pytest.raises(OperationalError, match="no such table"):
            MergeClear.objects.count()

    def test_require_current_schema_raises_actionable_error(self) -> None:
        with pytest.raises(SelfDbMigrationError) as exc:
            require_current_schema()
        message = str(exc.value)
        assert "unapplied migration" in message
        assert "ticket clear/merge" in message
        # The remediation points at the in-process self-rescue, not the old
        # `uv --directory <clone>` wrapper that resolved a different DB (#126).
        assert "t3 teatree db migrate" in message

    def test_ticket_clear_command_fails_closed_with_remediation(self) -> None:
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
        assert result["issued"] is False
        assert "unapplied migration" in str(result["error"])
        assert "t3 teatree db migrate" in str(result["error"])

    def test_ticket_merge_command_fails_closed_with_remediation(self) -> None:
        result = cast(
            "dict[str, object]",
            call_command("ticket", "merge", 1),
        )
        assert result["merged"] is False
        assert "unapplied migration" in str(result["error"])

    def test_review_record_command_fails_closed_with_remediation(self) -> None:
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
        assert result["recorded"] is False
        assert "unapplied migration" in str(result["error"])

    def test_doctor_surface_fails_and_names_pending_migrations(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = doctor_check_self_db_migrations()
        assert result is False
        out = buffer.getvalue()
        assert "unapplied migration" in out
        assert "0011_mergeclear_mergeaudit" in out

    def test_doctor_check_command_aggregates_pending_migrations(self) -> None:
        # The `t3 doctor check` aggregation wires the schema-guard surface
        # into its pass/fail result, so the gap shows at session start.
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            ok = doctor_check()
        assert ok is False
        assert "unapplied migration" in buffer.getvalue()


class DoctorCheckCurrentSchemaTest(TransactionTestCase):
    def test_doctor_check_passes_when_schema_current(self) -> None:
        assert doctor_check_self_db_migrations() is True

    def test_doctor_check_warns_but_does_not_fail_when_db_unreachable(self) -> None:
        # DB absent/offline at session start is a valid state — the doctor
        # check must WARN, not crash or block the session with a FAIL.
        buffer = io.StringIO()
        with (
            patch(
                "teatree.core.schema_guard.pending_migrations",
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
                "teatree.core.schema_guard.pending_migrations",
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
                "teatree.core.schema_guard.pending_migrations",
                side_effect=ProgrammingError("relation does not exist"),
            ),
            redirect_stdout(buffer),
        ):
            result = doctor_check_self_db_migrations()
        assert result is False
        assert "FAIL" in buffer.getvalue()
