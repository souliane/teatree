"""Regression test for #2919.

``worktree provision`` crashed on an unmigrated auto-isolated per-worktree
self-DB: the DB is seeded once from a canonical snapshot
(``teatree.paths._seed_isolated_db``) and never re-migrated on its own. When the
live editable install advances over a new migration the self-DB falls behind, and
a provisioning step that reads the ORM (e.g. ``get_effective_settings`` /
``resolve_step_timeout_seconds``) then crashed against the stale schema instead
of provisioning.

``WorktreeProvisionRunner.run()`` now self-heals the self-DB schema first
(mirroring the sanctioned merge path's ``require_current_schema``, #2006). These
tests pin that the runner (a) runs the self-heal pre-flight before provisioning,
and (b) fails loud, never a raw traceback, when the heal itself fails. That the
pre-flight actually brings a genuinely behind self-DB current is proven directly
against ``require_current_schema`` in ``test_schema_guard.py`` /
``test_schema_guard_migrate.py``; post the #2652/#3071 squash a single
``0001_initial`` means "one migration behind with the row still present" is no
longer a reachable mid-state, so the runner-level guard is that the pre-flight
runs and its failure is surfaced.
"""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.gates.schema_guard import SelfDbMigrationError
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import DbImportStrategy, OverlayBase, OverlayProvisioning, ProvisionStep
from teatree.core.runners import WorktreeProvisionRunner


class _DbImportOverlayProvisioning(OverlayProvisioning):
    def db_import_strategy(self, worktree: Worktree) -> DbImportStrategy | None:
        return {
            "kind": "fallback-chain",
            "source_database": "dev",
            "shared_postgres": True,
            "snapshot_tool": "dslr",
            "restore_order": [],
            "notes": [],
            "worktree_repo_path": worktree.repo_path,
        }

    def db_import(self, worktree: Worktree, **kwargs: Any) -> bool:
        return True


class _DbImportOverlay(OverlayBase):
    provisioning = _DbImportOverlayProvisioning()
    """Overlay whose DB-import strategy routes through ``run_timeboxed_db_import``."""

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []


class WorktreeProvisionRunsSchemaSelfHealTest(TestCase):
    """The runner runs the schema self-heal pre-flight before provisioning (#2919)."""

    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.wt_path = tmp_path / "worktree"
        self.wt_path.mkdir()

    def _make_worktree(self) -> Worktree:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/i/2919")
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="backend",
            branch="feature",
            db_name="wt_2919",
            extra={"worktree_path": str(self.wt_path)},
        )

    def test_run_invokes_the_schema_pre_flight(self) -> None:
        # The #2919 fix is the require_current_schema() pre-flight in run(); removing
        # it turns this assertion RED. The heal is stubbed to a no-op here (its own
        # behaviour is proven in test_schema_guard*.py) so the guard is that run()
        # calls it at all.
        worktree = self._make_worktree()

        with (
            patch("teatree.core.runners.worktree_provision.require_current_schema") as heal,
            patch("teatree.core.runners.worktree_provision._setup_worktree_dir", return_value=None),
            patch("teatree.utils.db.db_exists", return_value=False),
        ):
            result = WorktreeProvisionRunner(worktree, overlay=_DbImportOverlay()).run()

        assert heal.called, "run() must run the self-DB schema self-heal pre-flight"
        assert result.ok is True, result.detail


class WorktreeProvisionFailsLoudOnSelfHealFailureTest(TestCase):
    """A self-heal migrate failure must fail loud, never crash with a raw traceback."""

    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.wt_path = tmp_path / "worktree"
        self.wt_path.mkdir()

    def test_reports_the_migrate_failure_instead_of_raising(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/i/2919b")
        worktree = Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="backend",
            branch="feature",
            db_name="wt_2919b",
            extra={"worktree_path": str(self.wt_path)},
        )

        with patch(
            "teatree.core.runners.worktree_provision.require_current_schema",
            side_effect=SelfDbMigrationError("migrate exploded mid-apply"),
        ):
            result = WorktreeProvisionRunner(worktree, overlay=_DbImportOverlay()).run()

        assert result.ok is False
        assert "migrate exploded mid-apply" in result.detail
