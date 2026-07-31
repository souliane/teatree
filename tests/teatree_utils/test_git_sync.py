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

from teatree.utils.git_sync import fetch_all_prune, push
from tests.teatree_core.cleanup._shared import _GIT, _clean_env, _run_git


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
            [_GIT, "-C", str(self.repo), "for-each-ref", "--format=%(refname)", "refs/remotes"],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
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


class _RecordingRun:
    """A ``run_checked`` stand-in that records each call and reports success."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.envs: list[dict[str, str]] = []

    def __call__(
        self, cmd: list[str], *, env: dict[str, str] | None = None, **_: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(cmd)
        self.envs.append(dict(env or {}))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")


class TestPush:
    """The shared push helper must fail rather than block on a credential prompt (#3927)."""

    def test_runs_under_the_non_interactive_credential_env(self) -> None:
        recorder = _RecordingRun()

        with patch("teatree.utils.git_sync.run_checked", recorder):
            push(repo="/repo", branch="feature")

        assert recorder.envs[-1]["GIT_TERMINAL_PROMPT"] == "0"
        assert recorder.envs[-1]["GIT_ASKPASS"] == ""

    def test_pushes_the_named_branch_and_sets_upstream(self) -> None:
        recorder = _RecordingRun()

        with patch("teatree.utils.git_sync.run_checked", recorder):
            push(repo="/repo", remote="upstream", branch="feature")

        assert recorder.commands[-1] == ["git", "-C", "/repo", "push", "--set-upstream", "upstream", "feature"]

    def test_without_a_branch_it_only_sets_upstream(self) -> None:
        """No branch argument means no trailing refspec — git resolves HEAD itself."""
        recorder = _RecordingRun()

        with patch("teatree.utils.git_sync.run_checked", recorder):
            push(repo="/repo")

        assert recorder.commands[-1] == ["git", "-C", "/repo", "push", "--set-upstream", "origin"]
