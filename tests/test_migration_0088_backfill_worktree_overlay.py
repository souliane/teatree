"""Regression tests for ``core.0088_backfill_worktree_overlay_from_ticket``.

souliane/teatree#1397: worktree rows auto-detected via cwd were created with
``overlay=''`` while their ticket carried the real overlay, so the per-overlay
``max_concurrent_local_stacks`` gate missed them. 0088 backfills
``Worktree.overlay`` from ``ticket.overlay`` for every row left blank.

These tests pin the backfill behaviour: a blank-overlay worktree whose ticket
has an overlay is filled; a worktree whose ticket has no overlay is left blank;
a worktree that already carries an overlay is untouched.
"""

import importlib

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

# The migration module starts with a digit so it cannot be imported via the
# normal ``from ... import`` syntax; use ``importlib`` directly.
_migration_module = importlib.import_module(
    "teatree.core.migrations.0088_backfill_worktree_overlay_from_ticket",
)


class BackfillWorktreeOverlayMigrationTest(TransactionTestCase):
    """0088 must copy ``ticket.overlay`` onto blank ``Worktree.overlay`` rows."""

    _BEFORE = ("core", "0087_pause_all_loops")
    _AFTER = ("core", "0088_backfill_worktree_overlay_from_ticket")

    def _migrate(self, target: tuple[str, str]) -> "object":
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        executor.loader.build_graph()
        return executor.loader.project_state([target]).apps

    def _restore_head(self) -> None:
        """Re-apply every core migration so TransactionTestCase teardown flushes the real schema."""
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        core_leaves = [node for node in executor.loader.graph.leaf_nodes() if node[0] == "core"]
        executor.migrate(core_leaves)

    def _make_worktree(self, apps: "object", *, overlay: str, ticket_overlay: str, branch: str) -> "object":
        ticket_model = apps.get_model("core", "Ticket")
        worktree_model = apps.get_model("core", "Worktree")
        ticket = ticket_model.objects.create(
            issue_url=f"https://example.com/{ticket_overlay or 'x'}/issues/{branch}",
            overlay=ticket_overlay,
        )
        return worktree_model.objects.create(
            overlay=overlay,
            ticket=ticket,
            repo_path="backend",
            branch=branch,
            state="services_up",
        )

    def test_blank_overlay_is_backfilled_from_ticket(self) -> None:
        apps = self._migrate(self._BEFORE)
        worktree = self._make_worktree(apps, overlay="", ticket_overlay="t3-heavy", branch="1-feat")

        _migration_module._backfill_overlay(apps, connection.schema_editor())

        worktree.refresh_from_db()
        assert worktree.overlay == "t3-heavy"

        self._restore_head()

    def test_blank_overlay_with_blank_ticket_overlay_stays_blank(self) -> None:
        apps = self._migrate(self._BEFORE)
        worktree = self._make_worktree(apps, overlay="", ticket_overlay="", branch="2-feat")

        _migration_module._backfill_overlay(apps, connection.schema_editor())

        worktree.refresh_from_db()
        assert worktree.overlay == ""

        self._restore_head()

    def test_existing_overlay_is_left_untouched(self) -> None:
        apps = self._migrate(self._BEFORE)
        worktree = self._make_worktree(apps, overlay="t3-teatree", ticket_overlay="t3-heavy", branch="3-feat")

        _migration_module._backfill_overlay(apps, connection.schema_editor())

        worktree.refresh_from_db()
        assert worktree.overlay == "t3-teatree"

        self._restore_head()
