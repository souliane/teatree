"""Teardown must reap a branch-ref-gone worktree whose HEAD is in a remote.

The post-merge-delete debris that filled the disk to 98%: a worktree whose
local branch ref was deleted (a forge merge + branch cleanup) but whose on-disk
directory survives as an orphan. ``git rev-parse --abbrev-ref HEAD`` in such a
worktree reports a dangling symref (``DETACHED_HEAD``), and the data-loss probe
``git log HEAD --not --remotes`` exits 128 ("unknown revision"). The old
``_raise_if_unpushed`` translated that probe failure into an unconditional
"could not verify the branch is pushed" refusal, so the worktree was KEPT — even
though its work shipped on a remote. ``clean-all`` then skipped it every run and
the merged debris accumulated forever.

The fix: branch-ref-gone is itself the post-merge-delete signal. On the rc=128
probe failure the seam recovers the worktree's last HEAD SHA from its
per-worktree reflog and decides by containment in a remote — HEAD-in-remote is
positive proof the work is safe (reap); HEAD-in-NO-remote is genuinely-unsynced
local work (keep, the existing #706 safe default).

These exercise the fix against a real bare-remote git topology under
``tmp_path``: a worktree whose branch ref is dropped while HEAD is contained in
origin asserts reaped; a sibling whose HEAD is in no remote asserts kept.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.cleanup.cleanup import CleanupResult, _EffectiveTarget, cleanup_worktree
from teatree.core.cleanup.cleanup_orphan_ref import classify_orphan_ref, raise_or_reap_orphan_ref
from teatree.core.models import Ticket, Worktree
from teatree.utils import git
from teatree.utils.run import CommandFailedError
from tests.teatree_core.cleanup._shared import _GIT, _clean_env, _run_git


class _OrphanRefWorktreeFixture(TestCase):
    """A worktree whose local branch ref is deleted while the dir survives.

    Builds a real ``main`` clone with a bare ``origin`` remote, adds a worktree
    on ``feat-x``, then deletes ``refs/heads/feat-x`` directly — leaving the
    worktree's HEAD a dangling symref, exactly the orphan state a forge
    post-merge branch deletion produces locally. Subclasses decide whether the
    work was pushed (HEAD-in-remote) before the ref drop.
    """

    slug = "feat-x"

    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()

        self.remote = tmp_path / "remote.git"
        subprocess.run(
            [_GIT, "init", "-q", "--bare", "-b", "main", str(self.remote)],
            check=True,
            capture_output=True,
            env=_clean_env(),
        )

        self.repo_main = self.workspace / "myrepo"
        self.repo_main.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.repo_main)
        _run_git("config", "user.name", "t", cwd=self.repo_main)
        _run_git("remote", "add", "origin", str(self.remote), cwd=self.repo_main)
        (self.repo_main / "base.txt").write_text("base\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.repo_main)
        _run_git("commit", "-q", "-m", "initial", cwd=self.repo_main)
        _run_git("push", "-q", "origin", "main", cwd=self.repo_main)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)

        self.wt_path = self.workspace / self.slug / "myrepo"
        _run_git("worktree", "add", "-q", "-b", self.slug, str(self.wt_path), cwd=self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.wt_path)
        _run_git("config", "user.name", "t", cwd=self.wt_path)
        (self.wt_path / "feat.txt").write_text("feature work\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "feat: ship the feature", cwd=self.wt_path)

    def _drop_local_branch_ref(self) -> None:
        """Delete ``refs/heads/<slug>`` while the worktree still has it checked out.

        ``git branch -D`` refuses a checked-out branch, so this removes the ref
        directly — exactly the orphan state a forge post-merge branch deletion
        leaves locally, with the worktree HEAD now a dangling symref.
        """
        _run_git("update-ref", "-d", f"refs/heads/{self.slug}", cwd=self.repo_main)

    def _make_worktree(self) -> Worktree:
        ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/2707",
            state=Ticket.State.IN_REVIEW,
        )
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="myrepo",
            branch=self.slug,
            extra={"worktree_path": str(self.wt_path)},
        )

    def _cleanup(self, worktree: Worktree, *, pr_merged: bool = False) -> CleanupResult:
        with (
            patch("teatree.core.cleanup.cleanup.clone_root", return_value=self.workspace),
            patch("teatree.core.cleanup.cleanup.get_overlay_for_worktree") as mock_overlay,
            patch("teatree.core.cleanup.cleanup._branch_pr_is_merged", return_value=pr_merged),
        ):
            mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
            return cleanup_worktree(worktree, force=False, strict_hygiene=False)


class TestBranchRefGoneHeadInRemoteReaped(_OrphanRefWorktreeFixture):
    """Branch ref dropped while HEAD is contained in origin — REAP (the disk lever)."""

    def test_reaps_when_head_is_in_a_remote(self) -> None:
        # Push the work so HEAD's SHA is contained in origin/feat-x, THEN drop the
        # local branch ref — the orphan-with-shipped-work state.
        _run_git("push", "-q", "origin", self.slug, cwd=self.wt_path)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)
        self._drop_local_branch_ref()

        result = self._cleanup(self._make_worktree(), pr_merged=False)

        assert result.clean is True, f"Expected teardown to proceed, got errors: {result.errors}"
        assert not self.wt_path.exists(), "Worktree with HEAD-in-remote must be reaped, not skipped"


class TestBranchRefGoneHeadInNoRemoteKept(_OrphanRefWorktreeFixture):
    """Branch ref dropped while HEAD is on NO remote — KEEP (the #706 safe default)."""

    def test_keeps_when_head_is_in_no_remote(self) -> None:
        # NEVER push — the work exists only in this orphan worktree. Dropping the
        # branch ref must NOT make it reapable: it is genuinely-unsynced local work.
        self._drop_local_branch_ref()

        with pytest.raises(RuntimeError) as excinfo:
            self._cleanup(self._make_worktree(), pr_merged=False)

        message = str(excinfo.value)
        # The data-loss guard kept it — not the old cryptic probe-failure refusal.
        assert "could not verify the branch is pushed" not in message
        assert "on NO remote (data loss)" in message
        assert self.wt_path.exists(), "Genuinely-unsynced orphan must be kept, never destroyed"


def _target(*, ref: str, probe_repo: str = "/nonexistent") -> _EffectiveTarget:
    return _EffectiveTarget(ref=ref, probe_repo=probe_repo, branch_to_delete=None, label="feat-x")


class TestClassifyOrphanRef:
    """Unit coverage for the reap/keep classifier's fail-closed branches."""

    def test_non_detached_ref_is_not_an_orphan(self) -> None:
        # A named-branch target is not the dangling-HEAD orphan case — no recovery.
        decision = classify_orphan_ref(_target(ref="feat-x"))
        assert decision == decision.__class__(recovered_sha=None, in_remote=False, unsynced=[])

    def test_unrecoverable_head_yields_no_reap(self) -> None:
        with patch("teatree.core.cleanup.cleanup_orphan_ref.git.recovered_head_sha_after_ref_gone", return_value=None):
            decision = classify_orphan_ref(_target(ref=git.DETACHED_HEAD))
        assert decision.recovered_sha is None
        assert decision.in_remote is False

    def test_containment_probe_error_fails_closed(self) -> None:
        with (
            patch(
                "teatree.core.cleanup.cleanup_orphan_ref.git.recovered_head_sha_after_ref_gone", return_value="abc123"
            ),
            patch(
                "teatree.core.cleanup.cleanup_orphan_ref.git.commits_absent_from_all_remotes",
                side_effect=CommandFailedError(["git"], 128, "", "boom"),
            ),
        ):
            decision = classify_orphan_ref(_target(ref=git.DETACHED_HEAD))
        assert decision.recovered_sha == "abc123"
        assert decision.in_remote is False
        assert decision.unsynced == []


class TestRaiseOrReapOrphanRef:
    """Unit coverage for the rc=128 verdict — accurate messages per branch."""

    def _worktree(self) -> Worktree:
        ticket = Ticket(issue_url="https://example.com/issues/1", state=Ticket.State.IN_REVIEW)
        return Worktree(overlay="test", ticket=ticket, repo_path="myrepo", branch="feat-x")

    def _exc(self) -> CommandFailedError:
        return CommandFailedError(["git", "log"], 128, "", "fatal: ambiguous argument 'feat-x'")

    def test_unrecoverable_head_keeps_cryptic_probe_refusal(self) -> None:
        with (
            patch("teatree.core.cleanup.cleanup_orphan_ref.git.recovered_head_sha_after_ref_gone", return_value=None),
            pytest.raises(RuntimeError) as excinfo,
        ):
            raise_or_reap_orphan_ref(self._worktree(), _target(ref=git.DETACHED_HEAD), self._exc())
        assert "could not verify the branch is pushed" in str(excinfo.value)

    def test_recovered_but_on_no_remote_raises_accurate_data_loss(self) -> None:
        unsynced = ["aaa1111 first", "bbb2222 second", "ccc3333 third", "ddd4444 fourth"]
        with (
            patch(
                "teatree.core.cleanup.cleanup_orphan_ref.git.recovered_head_sha_after_ref_gone", return_value="aaa1111"
            ),
            patch("teatree.core.cleanup.cleanup_orphan_ref.git.commits_absent_from_all_remotes", return_value=unsynced),
            pytest.raises(RuntimeError) as excinfo,
        ):
            raise_or_reap_orphan_ref(self._worktree(), _target(ref=git.DETACHED_HEAD), self._exc())
        message = str(excinfo.value)
        assert "4 commit(s) on NO remote (data loss)" in message
        assert "…" in message  # the >_SUBJECT_PREVIEW_LIMIT truncation marker

    def test_head_in_remote_reaps_silently(self) -> None:
        with (
            patch(
                "teatree.core.cleanup.cleanup_orphan_ref.git.recovered_head_sha_after_ref_gone", return_value="aaa1111"
            ),
            patch("teatree.core.cleanup.cleanup_orphan_ref.git.commits_absent_from_all_remotes", return_value=[]),
        ):
            # Returns without raising — the reap verdict.
            raise_or_reap_orphan_ref(self._worktree(), _target(ref=git.DETACHED_HEAD), self._exc())
