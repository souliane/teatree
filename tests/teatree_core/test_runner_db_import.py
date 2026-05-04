"""Tests for ``WorktreeProvisionRunner._run_db_import`` skip semantics.

Regression guard for #484: when a worktree has no associated database (the
common case for frontend-only repos), the runner used to call
``overlay.db_import()`` anyway and log a misleading
``WARNING ... DB import failed for <repo> — continuing``. The runner now
skips ``_run_db_import`` entirely when ``worktree.db_name`` is empty.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import DbImportStrategy, OverlayBase, ProvisionStep
from teatree.core.overlay_loader import reset_overlay_cache
from teatree.core.runners import WorktreeProvisionRunner


class _RecordingOverlay(OverlayBase):
    """Overlay that records db_import calls and always returns a strategy."""

    def __init__(self) -> None:
        super().__init__()
        self.db_import_calls: int = 0

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []

    def get_db_import_strategy(self, worktree: Worktree) -> DbImportStrategy | None:
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
        self.db_import_calls += 1
        return True


@pytest.fixture(autouse=True)
def _clear_overlay_cache() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()


class TestRunnerSkipsDbImportWhenNoDbName(TestCase):
    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.wt_path = tmp_path / "worktree"
        self.wt_path.mkdir()

    def _make_worktree(self, *, db_name: str) -> Worktree:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/i/484")
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="frontend",
            branch="feature",
            db_name=db_name,
            extra={"worktree_path": str(self.wt_path)},
        )

    def test_skips_db_import_when_db_name_empty(self) -> None:
        """Frontend-style worktree (no DB) must not trigger overlay.db_import()."""
        worktree = self._make_worktree(db_name="")
        overlay = _RecordingOverlay()

        with patch("teatree.core.runners.worktree_provision._setup_worktree_dir"):
            WorktreeProvisionRunner(worktree, overlay=overlay).run()

        assert overlay.db_import_calls == 0

    def test_runs_db_import_when_db_name_set(self) -> None:
        """Backend-style worktree (with DB) still drives overlay.db_import()."""
        worktree = self._make_worktree(db_name="wt_484")
        overlay = _RecordingOverlay()

        with (
            patch("teatree.core.runners.worktree_provision._setup_worktree_dir"),
            patch("teatree.utils.db.db_exists", return_value=False),
        ):
            WorktreeProvisionRunner(worktree, overlay=overlay).run()

        assert overlay.db_import_calls == 1
