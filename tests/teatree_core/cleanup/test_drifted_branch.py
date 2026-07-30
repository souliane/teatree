"""Teardown must probe the worktree's ACTUAL branch/HEAD, not the DB-recorded slug.

``Worktree.branch`` (the DB row) can drift from the branch actually checked out
in the on-disk worktree: a real ``clean-all`` hit a group whose
``Worktree.branch`` was the ticket slug (``a-...-ticket``) while the worktree on
disk had a different branch (``techdebt-...``) checked out. The old teardown
seam trusted the slug, so:

- the data-loss probe ran ``git -C <repo_main> log <slug> --not --remotes``,
    which exits 128 ("unknown revision '<slug>'") and raises
    ``CommandFailedError`` → a cryptic "could not verify the branch is pushed
    (git probe failed: … unknown revision …)" refusal naming a non-existent
    branch; and
- under ``force=True`` the teardown's ``branch_delete(<slug>)`` silently
    no-op'd, leaving the REAL branch dangling after its worktree was removed.

These exercise the fix against a real bare-remote git topology under
``tmp_path``: the seam resolves the effective branch/HEAD from git and probes
the worktree dir directly, so it is robust to DB drift AND detached HEAD.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.cleanup.cleanup import CleanupResult, cleanup_worktree
from teatree.core.models import Ticket, Worktree
from tests.teatree_core.cleanup._shared import _GIT, _clean_env, _run_git


class _DriftedWorktreeFixture(TestCase):
    """A worktree whose DB ``branch`` slug differs from the checked-out branch.

    The fixture builds a real ``main`` clone with a bare ``origin`` remote, adds
    a worktree on the ticket-slug branch, then checks out a DIFFERENT real
    branch inside that worktree (the drift). The DB row keeps the slug; the
    worktree on disk has ``self.real_branch`` checked out — exactly the
    DB-vs-git drift the production seam must tolerate.
    """

    slug = "a-myrepo-7415-ticket"
    real_branch = "techdebt-ruff-RET504"

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

        # The worktree is added on the ticket SLUG (matches the DB row), then a
        # DIFFERENT real branch is checked out inside it — the DB-vs-git drift.
        self.wt_path = self.workspace / self.slug / "myrepo"
        _run_git("worktree", "add", "-q", "-b", self.slug, str(self.wt_path), cwd=self.repo_main)
        _run_git("checkout", "-q", "-b", self.real_branch, cwd=self.wt_path)

    def _make_worktree(self) -> Worktree:
        ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/7415",
            state=Ticket.State.IN_REVIEW,
        )
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="myrepo",
            branch=self.slug,  # DB records the slug; git has self.real_branch
            extra={"worktree_path": str(self.wt_path)},
        )

    def _cleanup(self, worktree: Worktree, *, force: bool = False, pr_merged: bool = False) -> CleanupResult:
        with (
            patch("teatree.core.cleanup.cleanup.clone_root", return_value=self.workspace),
            patch("teatree.core.cleanup.cleanup.get_overlay_for_worktree") as mock_overlay,
            patch("teatree.core.cleanup.cleanup._branch_pr_is_merged", return_value=pr_merged),
        ):
            mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
            return cleanup_worktree(worktree, force=force, strict_hygiene=False)

    def _branches(self) -> list[str]:
        return subprocess.run(
            [_GIT, "-C", str(self.repo_main), "branch", "--format=%(refname:short)"],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout.split()


class TestDriftedBranchUnpushedRefuses(_DriftedWorktreeFixture):
    """DRIFT + UNPUSHED — the reported bug. Teardown refuses ACCURATELY."""

    def test_refuses_naming_the_real_commit_not_unknown_revision(self) -> None:
        # The real (drifted) branch carries an unpushed commit.
        (self.wt_path / "feature.txt").write_text("real unpushed work\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "feat: real unpushed work on the drifted branch", cwd=self.wt_path)

        with pytest.raises(RuntimeError) as excinfo:
            self._cleanup(self._make_worktree(), force=False)

        message = str(excinfo.value)
        # The refusal is the accurate data-loss refusal, not the cryptic probe
        # failure naming a non-existent revision.
        assert "unknown revision" not in message
        assert "could not verify the branch is pushed" not in message
        assert "on NO remote (data loss)" in message
        # The worktree survives — nothing was destroyed.
        assert self.wt_path.exists()
        assert self.real_branch in self._branches()


class TestDriftedBranchFullyPushedProceeds(_DriftedWorktreeFixture):
    """DRIFT + FULLY PUSHED — must-not-block. Teardown proceeds."""

    def test_proceeds_when_real_branch_is_pushed(self) -> None:
        (self.wt_path / "feature.txt").write_text("pushed work\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "feat: pushed work", cwd=self.wt_path)
        _run_git("push", "-q", "origin", self.real_branch, cwd=self.wt_path)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)

        result = self._cleanup(self._make_worktree(), force=False)

        assert result.clean is True
        assert not self.wt_path.exists()
        # The real (checked-out) branch is the one removed by teardown.
        assert self.real_branch not in self._branches()


class TestDriftedBranchForceDeletesRealBranch(_DriftedWorktreeFixture):
    """DRIFT + force=True — worktree removed, the REAL (not slug) branch deleted.

    There is no recovery snapshot (the #1770 capture was removed): force=True is a
    deliberate hard-delete. The drift fix still holds — the EFFECTIVE branch is the
    one deleted, never the phantom DB slug.
    """

    def test_force_hard_deletes_the_real_branch(self) -> None:
        (self.wt_path / "feature.txt").write_text("real unpushed work\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "feat: real unpushed work", cwd=self.wt_path)

        result = self._cleanup(self._make_worktree(), force=True)

        assert result.clean is True
        assert not self.wt_path.exists()
        # The REAL (checked-out) branch must be the one deleted under force —
        # the pre-fix bug deleted the slug instead, leaving this dangling.
        assert self.real_branch not in self._branches()


class TestDetachedHeadUnpushedRefuses(_DriftedWorktreeFixture):
    """DETACHED HEAD with unpushed commits — refuses accurately (no crash)."""

    def test_detached_head_with_unpushed_commit_refuses(self) -> None:
        (self.wt_path / "feature.txt").write_text("detached work\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "feat: detached unpushed work", cwd=self.wt_path)
        # Detach: HEAD now points at a SHA, no branch name.
        _run_git("checkout", "-q", "--detach", "HEAD", cwd=self.wt_path)

        with pytest.raises(RuntimeError) as excinfo:
            self._cleanup(self._make_worktree(), force=False)

        message = str(excinfo.value)
        assert "unknown revision" not in message
        assert "on NO remote (data loss)" in message
        assert self.wt_path.exists()

    def test_detached_head_pushed_proceeds_under_strict_hygiene(self) -> None:
        # A detached HEAD whose tip is fully pushed must pass BOTH the #706
        # unpushed guard and the strict origin/main hygiene gate — the latter
        # has no named branch to classify, so it skips cleanly (no crash).
        (self.wt_path / "feature.txt").write_text("pushed detached work\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "feat: pushed detached work", cwd=self.wt_path)
        _run_git("push", "-q", "origin", self.real_branch, cwd=self.wt_path)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)
        _run_git("checkout", "-q", "--detach", "HEAD", cwd=self.wt_path)

        with (
            patch("teatree.core.cleanup.cleanup.clone_root", return_value=self.workspace),
            patch("teatree.core.cleanup.cleanup.get_overlay_for_worktree") as mock_overlay,
            patch("teatree.core.cleanup.cleanup._branch_pr_is_merged", return_value=False),
        ):
            mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
            result = cleanup_worktree(self._make_worktree(), force=False, strict_hygiene=True)

        assert result.clean is True
        assert not self.wt_path.exists()


class TestDetachedHeadForceHardDeletes(_DriftedWorktreeFixture):
    """DETACHED HEAD + force=True — a deliberate hard-delete (no recovery snapshot).

    Force is the explicit-abandon escape: the worktree is removed even on a detached
    HEAD with unpushed commits. There is no recovery bundle — the #1770 capture was
    removed; potentially-needed work is KEPT by the analyze-before-wipe reaper that
    fronts every automatic teardown, never reaching force.
    """

    def test_force_detached_head_removes_the_worktree(self) -> None:
        (self.wt_path / "feature.txt").write_text("orphan-only work\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "feat: reachable only from detached HEAD", cwd=self.wt_path)
        _run_git("checkout", "-q", "--detach", "HEAD", cwd=self.wt_path)

        result = self._cleanup(self._make_worktree(), force=True)

        assert result.clean is True
        assert not self.wt_path.exists()


class TestPhantomSlugBranchNotAGitRef(TestCase):
    """The literal production repro: the DB slug is NOT a git branch at all.

    In the real ``clean-all`` repro the worktree was provisioned directly on the
    real branch (``techdebt-ruff-RET504``); the ticket slug was only ever a
    DB/dir name, never a git ref. The old probe ran ``git log <slug> --not
    --remotes`` which exited 128 ("unknown revision '<slug>'"), surfacing the
    cryptic "could not verify the branch is pushed (git probe failed: …)"
    refusal. The fix probes the worktree dir's ``HEAD`` instead, so it sees the
    real unpushed work and refuses accurately.
    """

    slug = "a-product-svc-7415-ticket"
    real_branch = "techdebt-ruff-RET504"

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
        self.repo_main = self.workspace / "product-svc"
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

        # Worktree provisioned DIRECTLY on the real branch — the slug is never a
        # git ref, only the DB/dir name. ``git log <slug> …`` would exit 128.
        self.wt_path = self.workspace / self.slug / "product-svc"
        _run_git("worktree", "add", "-q", "-b", self.real_branch, str(self.wt_path), cwd=self.repo_main)
        (self.wt_path / "fix.txt").write_text("ruff RET504 fix\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "refactor: drop unnecessary assignment before return", cwd=self.wt_path)

    def _make_worktree(self) -> Worktree:
        ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/7415",
            state=Ticket.State.IN_REVIEW,
        )
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="product-svc",
            branch=self.slug,  # phantom — no such git branch exists
            extra={"worktree_path": str(self.wt_path)},
        )

    def _branches(self) -> list[str]:
        return subprocess.run(
            [_GIT, "-C", str(self.repo_main), "branch", "--format=%(refname:short)"],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout.split()

    def _cleanup(self, worktree: Worktree) -> CleanupResult:
        with (
            patch("teatree.core.cleanup.cleanup.clone_root", return_value=self.workspace),
            patch("teatree.core.cleanup.cleanup.get_overlay_for_worktree") as mock_overlay,
            patch("teatree.core.cleanup.cleanup._branch_pr_is_merged", return_value=False),
        ):
            mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
            return cleanup_worktree(worktree, force=False, strict_hygiene=False)

    def test_phantom_slug_refuses_with_accurate_message(self) -> None:
        with pytest.raises(RuntimeError) as excinfo:
            self._cleanup(self._make_worktree())

        message = str(excinfo.value)
        # The exact symptoms of the production bug must be gone.
        assert "unknown revision" not in message
        assert "could not verify the branch is pushed" not in message
        assert "on NO remote (data loss)" in message
        # Nothing destroyed; the real branch survives intact.
        assert self.wt_path.exists()
        assert self.real_branch in self._branches()
        assert self.slug not in self._branches()


class TestSquashMergedBranchNoRemoteRefPruned(TestCase):
    """#2205 — clean-all must prune a worktree whose branch was squash-merged and remote-deleted.

    When a PR is squash-merged (new SHA on main) and the source branch deleted on
    the remote, ``fetch --prune`` removes ``refs/remotes/origin/<branch>``.
    ``git log <branch> --not --remotes`` then reports the branch commits as
    "absent from all remotes" (the remote tracking ref is gone and the squash
    creates a distinct SHA on main), so ``commits_absent_from_all_remotes``
    returns non-empty even though the work is captured on ``origin/main``.

    The old code refused teardown (``_branch_pr_is_merged`` requires a host CLI
    absent in test/CI, so it silently skips). The fix adds an ancestry check:
    if HEAD is reachable from ``origin/<default>`` the work is already on the
    default branch and teardown is safe.
    """

    slug = "a-myrepo-8521-feat"

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

        # Create the feature branch, commit, push it (PR existed).
        self.wt_path = self.workspace / self.slug / "myrepo"
        _run_git("worktree", "add", "-q", "-b", self.slug, str(self.wt_path), cwd=self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.wt_path)
        _run_git("config", "user.name", "t", cwd=self.wt_path)
        (self.wt_path / "feat.txt").write_text("feature work\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "feat: ship the feature", cwd=self.wt_path)
        _run_git("push", "-q", "origin", self.slug, cwd=self.wt_path)

        # Squash-merge into main: a NEW sha on main captures the same tree,
        # but the branch commits are NOT ancestors of main.
        _run_git("checkout", "-q", "main", cwd=self.repo_main)
        _run_git("merge", "-q", "--squash", self.slug, cwd=self.repo_main)
        _run_git("commit", "-q", "-m", f"squash: {self.slug} (#2205)", cwd=self.repo_main)
        _run_git("push", "-q", "origin", "main", cwd=self.repo_main)

        # A forge squash-merge deletes the source ref REMOTELY (not via a local
        # push), so the local clone keeps a stale ``refs/remotes/origin/<slug>``
        # tracking ref until a later fetch prunes it. Deleting it on the bare
        # remote directly models that — the stale tracking ref is the
        # forge-CLI-free proof the branch was once pushed (the #2205 signal),
        # which ``cleanup._raise_if_unpushed`` samples before its own fetch.
        _run_git("update-ref", "-d", f"refs/heads/{self.slug}", cwd=self.remote)
        # The local branch ref, its worktree, and the stale remote tracking ref
        # all still exist (the local clone has not fetched/pruned yet).

    def _make_worktree(self) -> Worktree:
        ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/8521",
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

    def test_squash_merged_remote_deleted_branch_is_pruned(self) -> None:
        """The #2205 repro: teardown must proceed without forge CLI.

        ``commits_absent_from_all_remotes`` returns non-empty (the squash is a new
        SHA on main; the source ref was deleted) and ``_branch_pr_is_merged``
        returns False (no forge CLI in test). The teardown proceeds because the
        stale pre-fetch tracking ref (the branch was once pushed, then deleted on
        the forge) is positive merged-evidence AND the tree matches origin/main.
        """
        result = self._cleanup(self._make_worktree(), pr_merged=False)

        assert result.clean is True, f"Expected teardown to proceed, got errors: {result.errors}"
        assert not self.wt_path.exists(), "Worktree directory must be removed"


class TestLocalOnlyMatchingTreeNotPruned(TestCase):
    """#2205 Finding 1 (data loss): a never-pushed local-only branch whose tree matches main is KEPT.

    The confirmed data-loss false positive: a branch with genuinely local-only
    commits (never pushed to any remote) whose FINAL tree coincidentally equals
    ``origin/main`` — e.g. work added then fully reverted — passes
    ``git diff --quiet <ref> origin/main``. Tree equality alone was treated as
    "captured", bypassing the #706 guard and destroying the only copy of those
    commits (no ref, no reflog after worktree removal, nothing to ``fsck``).

    The branch is never pushed, so there is NO ``origin/<slug>`` tracking ref at
    any point and (absent a forge merged signal) NO positive merged-evidence.
    The guard must KEEP the worktree.
    """

    slug = "a-myrepo-9001-localonly"

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

        # Local-only branch: add work then fully revert it, NEVER push. The final
        # tree equals origin/main, but the two commits live nowhere but locally.
        self.wt_path = self.workspace / self.slug / "myrepo"
        _run_git("worktree", "add", "-q", "-b", self.slug, str(self.wt_path), cwd=self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.wt_path)
        _run_git("config", "user.name", "t", cwd=self.wt_path)
        (self.wt_path / "feat.txt").write_text("genuinely local work\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "add feat", cwd=self.wt_path)
        _run_git("rm", "-q", "feat.txt", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "revert - back to main tree", cwd=self.wt_path)

    def _make_worktree(self) -> Worktree:
        ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/9001",
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

    def test_local_only_matching_tree_branch_is_kept(self) -> None:
        """Tree-equality alone must NOT allow deletion — no merged-evidence → keep."""
        with pytest.raises(RuntimeError) as excinfo:
            self._cleanup(self._make_worktree(), pr_merged=False)

        message = str(excinfo.value)
        assert "on NO remote (data loss)" in message
        assert self.wt_path.exists(), "Local-only matching-tree branch must be kept, not destroyed"

    def test_local_only_matching_tree_pruned_only_with_forge_merged_evidence(self) -> None:
        """Tree-equality IS accepted once the forge confirms the PR merged."""
        result = self._cleanup(self._make_worktree(), pr_merged=True)

        assert result.clean is True, f"Expected teardown to proceed with merged-evidence, got: {result.errors}"
        assert not self.wt_path.exists()
