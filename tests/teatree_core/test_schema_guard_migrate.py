"""In-process self-DB migrate — the always-available stale-runtime-DB rescue (#126).

The sanctioned merge path refuses when ``pending_migrations()`` reports a
stale runtime self-DB, but every migrate wrapper before this either targeted
a *different* DB than the runtime process resolves (``t3 update`` ran
``uv --directory <clone> run migrate``, which auto-isolates to a sibling DB
for a worktree-anchored editable install) or was destructive (``resetdb``).
``migrate_self_db`` closes the gap: it applies migrations **in the running
process, against the exact connection** ``pending_migrations()`` reads — so
"migrate then re-check" is guaranteed to converge on the same DB. It is
non-destructive (rows survive), idempotent (a no-op on a current DB), and
fail-closed (a real migrate error raises).
"""

from unittest.mock import patch

import pytest
from django.db import OperationalError, connection, connections
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase

from teatree.core.gates.schema_guard import SelfDbMigrationError, migrate_self_db, pending_migrations

# A short contiguous tail to roll back so the DB is genuinely stale: the
# executor only reports a gap when no *descendant* is recorded, so we
# un-record from a fixed point through the leaf.
_ROLLBACK_FROM = "0040_miniloopmarker"


def _core_tail_from(name: str) -> list[str]:
    """Applied core migration ledger names ``>= name``, ascending."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT name FROM django_migrations WHERE app = 'core' AND name >= %s ORDER BY name",
            [name],
        )
        return [row[0] for row in cursor.fetchall()]


class MigrateSelfDbInProcessTest(TransactionTestCase):
    """``migrate_self_db`` converges the live test connection.

    It targets the same connection ``pending_migrations()`` inspects, so the
    migrate-then-recheck round-trip is guaranteed to converge on one DB.
    """

    def _make_stale(self) -> list[str]:
        """Roll the live DB back to before ``_ROLLBACK_FROM`` and return the gap.

        Reverses the migrations rather than just deleting ledger rows so the
        schema is genuinely behind (tables/columns absent), matching the real
        stale-runtime-DB state ``schema_guard`` guards against.
        """
        tail = _core_tail_from(_ROLLBACK_FROM)
        executor = MigrationExecutor(connection)
        # Migrate core back to the state just before the rollback point.
        target = ("core", _previous_core_migration(_ROLLBACK_FROM))
        executor.migrate([target])
        return tail

    def _restore_head(self) -> None:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        core_leaves = [node for node in executor.loader.graph.leaf_nodes() if node[0] == "core"]
        executor.migrate(core_leaves)

    def test_stale_db_is_brought_current(self) -> None:
        self.addCleanup(self._restore_head)
        self._make_stale()
        assert pending_migrations() != [], "precondition: DB must be stale"

        applied = migrate_self_db()

        assert pending_migrations() == [], "migrate_self_db must converge the live connection"
        assert applied, "must report the migration labels it applied"
        assert any(_ROLLBACK_FROM in label for label in applied)

    def test_idempotent_noop_on_current_db(self) -> None:
        # A current DB yields nothing pending; migrate returns an empty list
        # and does not error.
        assert pending_migrations() == []
        assert migrate_self_db() == []
        assert pending_migrations() == []

    def test_rows_survive_migrate(self) -> None:
        from teatree.core.models import Ticket  # noqa: PLC0415

        self.addCleanup(self._restore_head)
        ticket = Ticket.objects.create(overlay="t3-teatree", issue_url="https://example.com/issues/126")
        self._make_stale()

        migrate_self_db()

        ticket.refresh_from_db()
        assert ticket.issue_url == "https://example.com/issues/126", "non-destructive: existing rows survive"

    def test_fails_closed_when_migrate_errors(self) -> None:
        # A genuine migrate failure must raise SelfDbMigrationError, never be
        # swallowed — the consumer relies on a non-zero outcome to fail closed.
        self.addCleanup(self._restore_head)
        self._make_stale()  # ensure pending != [] so call_command is reached
        with (
            patch(
                "teatree.core.gates.schema_guard.call_command",
                side_effect=OperationalError("disk I/O error"),
            ),
            pytest.raises(SelfDbMigrationError) as exc,
        ):
            migrate_self_db()
        assert "disk I/O error" in str(exc.value)


def _previous_core_migration(name: str) -> str:
    """The applied core migration immediately preceding *name* in the ledger."""
    recorder = MigrationRecorder(connections["default"])
    applied = sorted(m_name for (app, m_name) in recorder.applied_migrations() if app == "core" and m_name < name)
    return applied[-1]
