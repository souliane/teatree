"""Regression tests for ``core.0066_sessionauditrecord``.

souliane/teatree#2192: the ``teatree_session_audit`` table was renamed across
two healed migration forks, so a persistent DB that applied an earlier name
already holds the table while the renamed migration still looks pending. The
:class:`CreateModelIfTableAbsent` subclass keeps the model in migration STATE
but no-ops the DATABASE create when the table is already present — idempotent
for a fresh DB, a recorder-only no-op for an old-name DB.

The idempotency path runs an introspection ``table_names`` lookup (#2279 nit 2:
its cursor is now managed by ``table_names`` itself, not leaked). These tests
exercise that path: a re-run against an already-created table is a no-op, not a
``table "teatree_session_audit" already exists`` crash.
"""

import importlib

from django.db import connection
from django.test import TransactionTestCase

_migration_module = importlib.import_module("teatree.core.migrations.0066_sessionauditrecord")


class CreateModelIfTableAbsentMigrationTest(TransactionTestCase):
    """0066 must no-op the DB create when the audit table already exists."""

    _OPERATION = _migration_module.CreateModelIfTableAbsent
    _TABLE = _migration_module._AUDIT_TABLE

    def _create_operation(self) -> object:
        migration = _migration_module.Migration("0066_sessionauditrecord", "core")
        return migration.operations[0]

    def test_database_forwards_is_a_noop_when_the_table_already_exists(self) -> None:
        # The table already exists (the test DB is at HEAD). Re-running the create
        # operation's database_forwards must introspect the live tables, see the
        # audit table present, and return without re-issuing CREATE TABLE (which
        # would raise "table already exists").
        with connection.schema_editor() as schema_editor:
            existing = set(schema_editor.connection.introspection.table_names())
            assert self._TABLE in existing

            operation = self._create_operation()
            assert isinstance(operation, self._OPERATION)
            operation.database_forwards("core", schema_editor, None, None)

            still_one = [t for t in schema_editor.connection.introspection.table_names() if t == self._TABLE]
            assert still_one == [self._TABLE]
