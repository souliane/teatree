"""Real-git behaviour of the remote-sync helpers.

Focused on :func:`fetch_all_prune`, the freshness precondition guarding the #706
data-loss probe. It must actually prune a tracking ref left stale by an upstream
deletion, and must report failure (never raise, never silently pass) when the
remote cannot be reached — destructive callers key their fail-closed branch on
that ``False``. The strict remote reads (:func:`remote_heads`, :func:`fetch_branch`)
raise instead, so "could not read origin" never reads as "origin has no such ref".
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.utils.git_sync import RemoteReadError, fetch_all_prune, fetch_branch, remote_heads
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


class TestStrictRemoteReads:
    @pytest.fixture(autouse=True)
    def _repo_with_origin(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.origin = tmp_path / "origin.git"
        self.origin.mkdir()
        _run_git("init", "-q", "--bare", "-b", "main", cwd=self.origin)
        self.repo = tmp_path / "clone"
        self.repo.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.repo)
        _run_git("config", "user.email", "t@t", cwd=self.repo)
        _run_git("config", "user.name", "t", cwd=self.repo)
        _run_git("remote", "add", "origin", str(self.origin), cwd=self.repo)
        _run_git("commit", "-q", "--allow-empty", "-m", "initial", cwd=self.repo)
        _run_git("push", "-q", "origin", "main", cwd=self.repo)

    def test_a_branch_counts_only_under_its_exact_ref(self) -> None:
        _run_git("push", "-q", "origin", "main:refs/heads/nest/refs/heads/feat", cwd=self.repo)
        assert remote_heads(str(self.repo), ["feat", "main"]) == {"main"}

        _run_git("push", "-q", "origin", "main:refs/heads/feat", cwd=self.repo)
        assert remote_heads(str(self.repo), ["feat", "main"]) == {"feat", "main"}

    def test_an_unreachable_remote_raises_gits_error(self) -> None:
        _run_git("remote", "set-url", "origin", str(self.tmp / "gone.git"), cwd=self.repo)

        with pytest.raises(RemoteReadError, match="does not appear to be a git repository"):
            remote_heads(str(self.repo), ["main"])
        with pytest.raises(RemoteReadError, match="does not appear to be a git repository"):
            fetch_branch(str(self.repo), "main")

    def test_a_credential_in_gits_error_is_redacted(self) -> None:
        _run_git("remote", "set-url", "origin", str(self.tmp / "token=s3cr3t" / "gone.git"), cwd=self.repo)

        with pytest.raises(RemoteReadError) as raised:
            remote_heads(str(self.repo), ["main"])

        assert "token=<redacted>" in str(raised.value)
        assert "s3cr3t" not in str(raised.value)

    def test_a_timeout_raises_rather_than_reading_as_absent(self) -> None:
        with (
            patch(
                "teatree.utils.git_run.run_bounded_group",
                side_effect=subprocess.TimeoutExpired(cmd="git ls-remote", timeout=1),
            ),
            pytest.raises(RemoteReadError, match="timed out after"),
        ):
            remote_heads(str(self.repo), ["main"])

    def test_fetch_branch_retries_a_fetch_that_lost_the_ref_to_a_concurrent_one(self) -> None:
        # Created in origin so the clone has no tracking ref; git 2.43 skips the hook for an up-to-date ref.
        _run_git("update-ref", "refs/heads/moved", "refs/heads/main", cwd=self.origin)
        hooks = self.tmp / "hooks"
        hooks.mkdir()
        hook = hooks / "reference-transaction"
        hook.write_text(
            '#!/bin/sh\n[ "$1" = prepared ] || exit 0\n[ -e "$0.failed" ] && exit 0\ntouch "$0.failed"\nexit 1\n',
            encoding="utf-8",
        )
        hook.chmod(0o755)
        _run_git("config", "core.hooksPath", str(hooks), cwd=self.repo)

        fetch_branch(str(self.repo), "moved")

        assert (hooks / "reference-transaction.failed").exists(), "the first fetch never failed"
        tracked = subprocess.run(
            [_GIT, "-C", str(self.repo), "rev-parse", "--verify", "--quiet", "refs/remotes/origin/moved"],
            check=False,
            capture_output=True,
            env=_clean_env(),
        )
        assert tracked.returncode == 0, "the retried fetch did not land the ref"

    def test_a_timed_out_fetch_is_not_retried(self) -> None:
        with (
            patch(
                "teatree.utils.git_run.run_bounded_group",
                side_effect=subprocess.TimeoutExpired(cmd="git fetch", timeout=1),
            ) as bounded,
            pytest.raises(RemoteReadError, match="timed out after"),
        ):
            fetch_branch(str(self.repo), "main")

        assert bounded.call_count == 1

    def test_fetch_branch_reaches_a_branch_a_single_branch_clone_never_tracked(self) -> None:
        _run_git("push", "-q", "origin", "main:refs/heads/other", cwd=self.repo)
        narrow = self.tmp / "narrow"
        _run_git("clone", "-q", "--single-branch", "-b", "main", str(self.origin), str(narrow), cwd=self.tmp)

        fetch_branch(str(narrow), "other")

        refs = subprocess.run(
            [_GIT, "-C", str(narrow), "for-each-ref", "--format=%(refname)", "refs/remotes"],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout
        assert "refs/remotes/origin/other" in refs
