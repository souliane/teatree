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

Since the #2652 squash ``core`` is a single ``0001_initial``, so the only
genuinely-stale state a real reverse migrate can reach is the pre-initial one
(``zero``), from which ``migrate_self_db`` re-applies the initial and seeds.
"""

from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import OperationalError
from django.test import TransactionTestCase

from teatree.core.gates.schema_guard import SelfDbMigrationError, migrate_self_db, pending_migrations


class MigrateSelfDbInProcessTest(TransactionTestCase):
    """``migrate_self_db`` converges the live test connection.

    It targets the same connection ``pending_migrations()`` inspects, so the
    migrate-then-recheck round-trip is guaranteed to converge on one DB.
    """

    def _make_stale(self) -> list[str]:
        """Reverse the live DB to ``zero`` so it is genuinely behind.

        The squashed graph's only reverse target is ``zero``: un-applying
        ``0001_initial`` really drops the core tables (schema genuinely
        behind), the exact stale-runtime-DB state ``schema_guard`` guards
        against. ``migrate_self_db`` then re-applies the initial forward.
        """
        call_command("migrate", "core", "zero", "--no-input", verbosity=0)
        return pending_migrations()

    def _restore_head(self) -> None:
        call_command("migrate", "core", "--no-input", verbosity=0)

    def test_stale_db_is_brought_current(self) -> None:
        self.addCleanup(self._restore_head)
        self._make_stale()
        assert pending_migrations() != [], "precondition: DB must be stale"

        applied = migrate_self_db()

        assert pending_migrations() == [], "migrate_self_db must converge the live connection"
        assert applied, "must report the migration labels it applied"
        assert any("0001_initial" in label for label in applied)

    def test_idempotent_noop_on_current_db(self) -> None:
        # A current DB yields nothing pending; migrate returns an empty list
        # and does not error.
        assert pending_migrations() == []
        assert migrate_self_db() == []
        assert pending_migrations() == []

    def test_migrate_is_non_destructive_to_existing_rows(self) -> None:
        # ``migrate_self_db`` runs a forward ``migrate`` that never drops live
        # rows. On the squashed single-migration graph the only reverse target
        # is ``zero`` (which recreates the schema from scratch), so the
        # non-destructive guarantee is pinned on the always-available
        # invocation: a no-op migrate against a current DB leaves existing rows
        # exactly as they were.
        from teatree.core.models import Ticket  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="t3-teatree", issue_url="https://example.com/issues/126")

        assert migrate_self_db() == [], "current DB: migrate is a no-op"

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
