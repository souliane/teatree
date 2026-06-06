"""``t3 <overlay> db migrate`` — in-process self-DB migrate command (#126).

The sanctioned merge path refuses on a stale runtime self-DB, but no
sanctioned, non-destructive migrate targeted *that* DB: ``t3 update`` ran
``uv --directory <clone> run migrate`` (a different, auto-isolated DB for a
worktree-anchored editable install) and ``resetdb`` is destructive. This
command closes the gap — it delegates to
:func:`teatree.core.gates.schema_guard.migrate_self_db`, which migrates the exact
connection the gate reads, in the running process.
"""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase

from teatree.core.models import Ticket


class DbMigrateCommandTest(TransactionTestCase):
    """``db migrate`` brings the live (gate) connection current, non-destructively."""

    def test_migrate_reports_already_current_when_nothing_pending(self) -> None:
        out = StringIO()
        call_command("db", "migrate", stdout=out)
        # The test DB is at head, so nothing is pending — the command must
        # report a clean no-op, not error.
        assert "already" in out.getvalue().lower() or "current" in out.getvalue().lower()

    def test_migrate_delegates_to_in_process_self_rescue(self) -> None:
        # The command must route through migrate_self_db (the in-process
        # rescue against the live connection), never a subprocess wrapper.
        with patch(
            "teatree.core.management.commands.db.migrate_self_db",
            return_value=["core.0041_resource_pressure_marker"],
        ) as mocked:
            out = StringIO()
            call_command("db", "migrate", stdout=out)
        assert mocked.called
        assert "core.0041_resource_pressure_marker" in out.getvalue()

    def test_migrate_preserves_existing_rows(self) -> None:
        # Non-destructive: a row written before migrate survives it. On a
        # head DB this is a no-op migrate, but it still must not drop data.
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.STARTED)
        call_command("db", "migrate", stdout=StringIO())
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED
        assert connection.vendor == "sqlite"

    def test_migrate_fails_closed_when_underlying_migrate_errors(self) -> None:
        import pytest  # noqa: PLC0415

        from teatree.core.gates.schema_guard import SelfDbMigrationError  # noqa: PLC0415

        with (
            patch(
                "teatree.core.management.commands.db.migrate_self_db",
                side_effect=SelfDbMigrationError("disk I/O error"),
            ),
            pytest.raises(SystemExit),
        ):
            call_command("db", "migrate", stdout=StringIO(), stderr=StringIO())
