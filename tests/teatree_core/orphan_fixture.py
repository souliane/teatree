"""Real-git fixture for the RAW (unregistered) worktree passes — reaper and emit share it.

Both passes answer their questions about the same object, so they are driven against one
fixture: a main clone with a bare ``origin`` it can push to, plus a helper that adds a
``git worktree`` carrying NO teatree ``Worktree`` row. Hoisted out of the reaper's own test
module when ``workspace emit`` became the second consumer (#4579).
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.management.commands._workspace.orphan_worktrees import reap_orphan_raw_worktrees
from tests.teatree_core.cleanup._shared import _GIT, _clean_env, _run_git


class OrphanWorktreeFixture(TestCase):
    """A main clone + a bare ``origin`` it can push to, under ``tmp_path``."""

    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.captures = tmp_path / "captures"
        self.origin = tmp_path / "origin.git"
        self.origin.mkdir()
        _run_git("init", "-q", "--bare", "-b", "main", cwd=self.origin)
        self.repo_main = self.workspace / "myrepo"
        self.repo_main.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.repo_main)
        _run_git("config", "user.name", "t", cwd=self.repo_main)
        _run_git("remote", "add", "origin", str(self.origin), cwd=self.repo_main)
        (self.repo_main / "README").write_text("x")
        _run_git("add", "-A", cwd=self.repo_main)
        _run_git("commit", "-q", "-m", "initial", cwd=self.repo_main)
        _run_git("push", "-q", "-u", "origin", "main", cwd=self.repo_main)

    def _add_orphan(self, branch: str, *, files: dict[str, str] | None = None, detach: bool = False) -> Path:
        """Create a raw ``git worktree`` (no DB row) on ``branch`` with optional commits."""
        wt_path = self.workspace / branch / "myrepo"
        if detach:
            _run_git("worktree", "add", "-q", "--detach", str(wt_path), "HEAD", cwd=self.repo_main)
        else:
            _run_git("worktree", "add", "-q", "-b", branch, str(wt_path), cwd=self.repo_main)
        for name, content in (files or {}).items():
            (wt_path / name).write_text(content)
            _run_git("add", "-A", cwd=wt_path)
            _run_git("commit", "-q", "-m", f"add {name}", cwd=wt_path)
        return wt_path

    def _registered_paths(self) -> str:
        return subprocess.run(
            [_GIT, "-C", str(self.repo_main), "worktree", "list"],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout

    def _reap(self, *, dry_run: bool = False, clean_ignored: bool = False) -> list[str]:
        # Force cwd-based clone discovery onto the tmp main clone, and keep the
        # pre-reap capture's bundles inside tmp_path instead of the real data dir.
        with (
            patch(
                "teatree.core.management.commands._workspace.orphan_worktrees.is_clean_ignored",
                return_value=clean_ignored,
            ),
            patch(
                "teatree.core.management.commands._workspace.orphan_worktrees.Path.cwd",
                return_value=self.repo_main,
            ),
            patch("teatree.core.cleanup.unshipped_work.get_data_dir", return_value=self.captures),
        ):
            return reap_orphan_raw_worktrees(self.workspace, dry_run=dry_run)
