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

A real reverse migrate can leave ``core`` behind at any point in its
migration graph; the tests below exercise the pre-initial state (``zero``),
the state a keystone merge of a migration-adding PR leaves the runtime
self-DB in, from which ``migrate_self_db`` re-applies the whole graph.

Every test below drives its stale/restore cycle against a **private,
file-backed SQLite connection** registered per test — never the shared,
xdist-worker-lifetime-reused ``default`` test database (#2915). Reverse-
migrating ``core`` to ``zero`` against ``default`` risked leaving every other
test in the same worker permanently stale if a cleanup was ever interrupted
(a ``pytest-timeout`` firing mid-restore, an OOM-kill); a private alias
confines that risk to the one throwaway file this test itself creates and
tears down.
"""

from unittest.mock import patch

import pytest
from django.db import OperationalError

from teatree.core.gates.schema_guard import SelfDbMigrationError, migrate_self_db, pending_migrations
from teatree.core.models import Ticket
from tests.teatree_core.conftest import SchemaGuardAlias


class TestMigrateSelfDbInProcess:
    """``migrate_self_db`` converges the connection named by its ``alias`` arg.

    Each test registers its own private, file-backed SQLite alias (via the
    shared ``schema_guard_alias`` fixture) — never the shared ``default``
    connection every other test in the xdist worker reuses — so a crashed
    reverse-migrate/restore cycle cannot corrupt unrelated tests (#2915).
    """

    def test_stale_db_is_brought_current(self, schema_guard_alias: SchemaGuardAlias) -> None:
        alias = schema_guard_alias.make_stale()
        assert pending_migrations(alias) != [], "precondition: DB must be stale"

        applied = migrate_self_db(alias)

        assert pending_migrations(alias) == [], "migrate_self_db must converge the aliased connection"
        assert applied, "must report the migration labels it applied"
        assert any("0001_initial" in label for label in applied)

    def test_idempotent_noop_on_current_db(self, schema_guard_alias: SchemaGuardAlias) -> None:
        alias = schema_guard_alias.register_current()
        # A current DB yields nothing pending; migrate returns an empty
        # list and does not error.
        assert pending_migrations(alias) == []
        assert migrate_self_db(alias) == []
        assert pending_migrations(alias) == []

    def test_migrate_is_non_destructive_to_existing_rows(self, schema_guard_alias: SchemaGuardAlias) -> None:
        alias = schema_guard_alias.register_current()
        ticket = Ticket.objects.using(alias).create(overlay="t3-teatree", issue_url="https://example.com/issues/126")

        assert migrate_self_db(alias) == [], "current DB: migrate is a no-op"

        ticket.refresh_from_db()
        assert ticket.issue_url == "https://example.com/issues/126", "non-destructive: existing rows survive"

    def test_fails_closed_when_migrate_errors(self, schema_guard_alias: SchemaGuardAlias) -> None:
        # A genuine migrate failure must raise SelfDbMigrationError, never be
        # swallowed — the consumer relies on a non-zero outcome to fail closed.
        alias = schema_guard_alias.make_stale()
        assert pending_migrations(alias) != [], "precondition: ensure call_command is reached"
        with (
            patch(
                "teatree.core.gates.schema_guard.call_command",
                side_effect=OperationalError("disk I/O error"),
            ),
            pytest.raises(SelfDbMigrationError) as exc,
        ):
            migrate_self_db(alias)
        assert "disk I/O error" in str(exc.value)
