"""Regression tests for the read-only DB-introspection entrypoint (#774).

Agents debugging lifecycle/gate state previously had to detour through
``uv run python manage.py shell -c "..."``. ``t3 <ov> db query`` /
``t3 <ov> db shell`` give a first-class, read-only, JSON-emitting entrypoint
that resolves the *same* control DB the shipping gate reads (the proxy binds
it identically to ``pr create`` / ``visit-phase``).

The contract pinned here:
- ``db query`` runs a read-only SQL statement and emits rows as JSON.
- a write/DDL statement (INSERT/UPDATE/DELETE/DROP) is REFUSED, never executed.
- the query runs against the live Django connection (the gate's DB), so
introspection matches gate behaviour by construction.
"""

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import TestCase

from teatree.core.management.commands.db import _read_only_guard_sql
from teatree.core.models import Session, Ticket


class TestReadOnlyGuardSql:
    """``_read_only_guard_sql`` — vendor → enforced-read-only SQL (#774 F1).

    Pure mapping, so the Postgres branch (the overlay DB the gate uses,
    unreachable from the SQLite test harness) is still covered here.
    """

    def test_postgresql_uses_read_only_transaction(self) -> None:
        assert _read_only_guard_sql("postgresql") == ("SET TRANSACTION READ ONLY", None)

    def test_sqlite_toggles_query_only_pragma(self) -> None:
        assert _read_only_guard_sql("sqlite") == (
            "PRAGMA query_only = ON",
            "PRAGMA query_only = OFF",
        )

    def test_unknown_vendor_has_no_db_level_guard(self) -> None:
        # Falls back to the leading-keyword pre-filter only.
        assert _read_only_guard_sql("oracle") == (None, None)


class TestRunReadOnlyVendorFallback(TestCase):
    """``_run_read_only`` skips guard SQL when the vendor has none (#774 F1)."""

    def test_no_guard_sql_when_vendor_unknown(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.core.management.commands import db as db_cmd  # noqa: PLC0415

        Ticket.objects.create(overlay="novendor", state=Ticket.State.STARTED)
        # An unknown vendor yields (None, None): the enter/exit guard
        # statements are skipped (the `is not None` False branches) and the
        # SELECT still runs on the underlying SQLite connection.
        with patch.object(db_cmd, "_read_only_guard_sql", return_value=(None, None)):
            rows = db_cmd._run_read_only("SELECT COUNT(*) AS n FROM teatree_ticket")
        assert rows == [{"n": 1}]


class TestDbQuery(TestCase):
    """``db query`` — read-only SQL → JSON, write-refusing (#774)."""

    def _run_query(self, sql: str) -> str:
        out = StringIO()
        call_command("db", "query", sql, stdout=out)
        return out.getvalue()

    def test_select_emits_rows_as_json(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        Session.objects.create(ticket=ticket, overlay="test")

        raw = self._run_query("SELECT id, state, overlay FROM teatree_ticket")
        payload = json.loads(raw)

        assert payload == [{"id": ticket.pk, "state": "reviewed", "overlay": "test"}]

    def test_empty_result_is_empty_json_array(self) -> None:
        raw = self._run_query("SELECT id FROM teatree_ticket WHERE id = -1")
        assert json.loads(raw) == []

    def test_write_statement_is_refused_and_not_executed(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.CODED)

        with pytest.raises(SystemExit):
            call_command(
                "db",
                "query",
                "UPDATE teatree_ticket SET state = 'shipped'",
                stdout=StringIO(),
            )

        ticket.refresh_from_db()
        # The write must NOT have landed — introspection is read-only.
        assert ticket.state == Ticket.State.CODED

    def test_ddl_statement_is_refused(self) -> None:
        with pytest.raises(SystemExit):
            call_command("db", "query", "DROP TABLE teatree_ticket", stdout=StringIO())
        # Table still present => DDL never executed.
        assert Ticket.objects.count() == 0

    def test_data_modifying_cte_is_refused_and_not_executed(self) -> None:
        """A data-modifying ``WITH … DELETE … RETURNING`` CTE is refused (#774 F1)."""
        Ticket.objects.create(overlay="cte", state=Ticket.State.CODED)

        with pytest.raises(SystemExit):
            call_command(
                "db",
                "query",
                "WITH t AS (DELETE FROM teatree_ticket RETURNING *) SELECT * FROM t",
                stdout=StringIO(),
            )

        # The DELETE must NOT have landed — the row survives.
        assert Ticket.objects.count() == 1

    def test_select_into_is_refused_and_creates_no_table(self) -> None:
        """``SELECT … INTO newtbl`` (table-creating) is refused (#774 F1)."""
        Ticket.objects.create(overlay="into", state=Ticket.State.CODED)

        with pytest.raises(SystemExit):
            call_command(
                "db",
                "query",
                "SELECT * INTO db_query_leak_tbl FROM teatree_ticket",
                stdout=StringIO(),
            )

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='db_query_leak_tbl'",
            )
            assert cursor.fetchone() is None

    def test_writable_pragma_setter_is_refused(self) -> None:
        """A PRAGMA setter (``PRAGMA writable_schema=1``) is refused (#774 F1).

        The read-only transaction does not block a connection-level PRAGMA
        switch, so the setter form (PRAGMA with ``=``) must be rejected by
        the leading-keyword pre-filter instead.
        """
        with pytest.raises(SystemExit):
            call_command("db", "query", "pragma writable_schema=1", stdout=StringIO())

    def test_read_only_introspection_pragma_is_allowed(self) -> None:
        """A non-setter PRAGMA (no ``=``) stays usable for introspection.

        The setter rejection must not over-block ``PRAGMA table_info``.
        """
        raw = self._run_query("PRAGMA table_info(teatree_ticket)")
        columns = json.loads(raw)
        assert any(col["name"] == "state" for col in columns)

    def test_blank_sql_is_refused(self) -> None:
        """Empty / whitespace-only / bare ``;`` SQL is refused (#774 F2).

        Covers the ``not stripped`` branch of the pre-filter.
        """
        for blank in ("", "   ", "  ;  "):
            with pytest.raises(SystemExit):
                call_command("db", "query", blank, stdout=StringIO())

    def test_query_runs_against_the_live_gate_connection(self) -> None:
        """Query sees ORM-written rows: same connection the gate reads.

        Rows written via the ORM in this transaction are visible to the
        query — proving it uses the same connection the gate reads, not a
        separately-resolved sqlite file (the #774 asymmetry).
        """
        Ticket.objects.create(overlay="alpha", state=Ticket.State.STARTED)
        Ticket.objects.create(overlay="beta", state=Ticket.State.STARTED)

        raw = self._run_query("SELECT COUNT(*) AS n FROM teatree_ticket")
        assert json.loads(raw) == [{"n": 2}]
        # Sanity: the command's connection IS the test connection.
        assert connection.vendor == "sqlite"


class TestDbShell(TestCase):
    """``db shell`` — interactive Django shell with models pre-imported (#774)."""

    def test_shell_invokes_django_shell_with_preimport(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch("teatree.core.management.commands.db.call_command") as mocked:
            call_command("db", "shell")

        # Delegates to Django's own shell so the resolved (gate) DB and
        # connection are reused; pre-imports models for interactive use.
        assert mocked.call_args is not None
        args, _kwargs = mocked.call_args
        assert args[0] == "shell"
