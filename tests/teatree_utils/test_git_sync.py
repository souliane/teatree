"""Real-git behaviour of the remote-sync helpers.

Focused on :func:`fetch_all_prune`, the freshness precondition guarding the #706
data-loss probe. It must actually prune a tracking ref left stale by an upstream
deletion, and must report failure (never raise, never silently pass) when the
remote cannot be reached — destructive callers key their fail-closed branch on
that ``False``.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.utils.git_sync import fetch_all_prune
from tests.teatree_core.cleanup._shared import _run_git


class TestFetchAllPrune:
    @pytest.fixture(autouse=True)
    def _repo_with_origin(self, tmp_path: Path) -> None:
        self.origin = tmp_path / "origin.git"
        self.origin.mkdir()
        _run_git("init", "-q", "--bare", "-b", "main", cwd=self.origin)
        self.repo = tmp_path / "clone"
        self.repo.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.repo)
        _run_git("config", "user.email", "t@t", cwd=self.repo)
        _run_git("config", "user.name", "t", cwd=self.repo)
        _run_git("remote", "add", "origin", str(self.origin), cwd=self.repo)
        (self.repo / "README").write_text("x")
        _run_git("add", "-A", cwd=self.repo)
        _run_git("commit", "-q", "-m", "initial", cwd=self.repo)
        _run_git("push", "-q", "-u", "origin", "main", cwd=self.repo)

    def _tracking_refs(self) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), "for-each-ref", "--format=%(refname)", "refs/remotes"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def test_prunes_a_tracking_ref_left_stale_by_an_upstream_deletion(self) -> None:
        _run_git("checkout", "-q", "-b", "feature", cwd=self.repo)
        _run_git("push", "-q", "-u", "origin", "feature", cwd=self.repo)
        assert "refs/remotes/origin/feature" in self._tracking_refs()
        # Delete upstream ONLY (as a forge auto-delete-on-merge does), so this
        # clone keeps a tracking ref that no longer exists on the remote.
        _run_git("update-ref", "-d", "refs/heads/feature", cwd=self.origin)
        assert "refs/remotes/origin/feature" in self._tracking_refs(), "precondition: ref should still be stale"

        assert fetch_all_prune(str(self.repo)) is True
        assert "refs/remotes/origin/feature" not in self._tracking_refs()

    def test_returns_false_for_an_unreachable_remote(self, tmp_path: Path) -> None:
        _run_git("remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"), cwd=self.repo)
        assert fetch_all_prune(str(self.repo)) is False

    def test_returns_false_on_timeout_rather_than_raising(self) -> None:
        """A hung fetch must fail closed, not propagate and abort the whole sweep."""
        with patch(
            "teatree.utils.git_sync.run_allowed_to_fail",
            side_effect=subprocess.TimeoutExpired(cmd="git fetch", timeout=1),
        ):
            assert fetch_all_prune(str(self.repo)) is False
