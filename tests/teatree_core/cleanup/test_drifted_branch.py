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

from teatree.core.cleanup import CleanupResult, cleanup_worktree
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
    def _tmp_workspace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.temp_root = tmp_path / "systmp"
        self.temp_root.mkdir()
        monkeypatch.setattr(
            "teatree.core.worktree_snapshot.tempfile.gettempdir",
            lambda: str(self.temp_root),
        )

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
            patch("teatree.core.cleanup.load_config") as mock_config,
            patch("teatree.core.cleanup.get_overlay") as mock_overlay,
            patch("teatree.core.cleanup._branch_pr_is_merged", return_value=pr_merged),
        ):
            mock_config.return_value.user.workspace_dir = self.workspace
            mock_overlay.return_value.get_cleanup_steps.return_value = []
            return cleanup_worktree(worktree, force=force, strict_hygiene=False)

    def _branches(self) -> list[str]:
        return subprocess.run(
            [_GIT, "-C", str(self.repo_main), "branch", "--format=%(refname:short)"],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout.split()

    def _recovery_dirs(self) -> list[Path]:
        return sorted(p for p in self.temp_root.iterdir() if p.is_dir() and p.name.startswith("t3-recover-"))


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
    """DRIFT + force=True — recovery captured, worktree removed, REAL branch deleted."""

    def test_force_captures_recovery_and_deletes_the_real_branch(self) -> None:
        (self.wt_path / "feature.txt").write_text("real unpushed work\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "feat: real unpushed work", cwd=self.wt_path)

        result = self._cleanup(self._make_worktree(), force=True)

        assert result.clean is True
        assert not self.wt_path.exists()
        # The REAL (checked-out) branch must be the one deleted under force —
        # the pre-fix bug deleted the slug instead, leaving this dangling.
        assert self.real_branch not in self._branches()
        # A recovery bundle was captured before the destructive remove.
        dirs = self._recovery_dirs()
        assert len(dirs) == 1, f"exactly one recovery dir expected, got {dirs}"
        bundle = dirs[0] / "branch.bundle"
        assert bundle.is_file(), "branch bundle missing"
        log = subprocess.run(
            [_GIT, "-C", str(self.repo_main), "bundle", "list-heads", str(bundle)],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout
        # The bundle is of the REAL branch, not the slug.
        assert self.real_branch in log


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
            patch("teatree.core.cleanup.load_config") as mock_config,
            patch("teatree.core.cleanup.get_overlay") as mock_overlay,
            patch("teatree.core.cleanup._branch_pr_is_merged", return_value=False),
        ):
            mock_config.return_value.user.workspace_dir = self.workspace
            mock_overlay.return_value.get_cleanup_steps.return_value = []
            result = cleanup_worktree(self._make_worktree(), force=False, strict_hygiene=True)

        assert result.clean is True
        assert not self.wt_path.exists()


class TestDetachedHeadForceCapturesTheRealCommits(_DriftedWorktreeFixture):
    """DETACHED HEAD + force=True — the recovery bundle must hold the detached commits.

    Force skips the data-loss guard, so the recovery capture (#835/#1506) is the
    ONLY protection. The detached commit is reachable from no named branch, so a
    bundle of any slug/branch ref would miss it entirely (the pre-fix path
    collapsed ``HEAD`` to the DB slug and bundled that → zero recovery + the
    detached commit lost after gc/ref-prune). The capture must bundle ``HEAD``
    from the worktree dir, where it resolves to the detached commit.
    """

    def _corrupt_head_commit_object(self) -> None:
        """Make ``git -C <wt_path>`` unable to read HEAD's commit — both bundle and probe error."""
        sha = subprocess.run(
            [_GIT, "-C", str(self.wt_path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout.strip()
        obj = self.repo_main / ".git" / "objects" / sha[:2] / sha[2:]
        obj.chmod(0o644)
        obj.write_bytes(b"corrupt")

    def test_force_detached_head_bundle_contains_the_detached_commit(self) -> None:
        # A commit reachable ONLY from the detached HEAD, then a clean tree so the
        # ONLY thing to lose is the unpushed commit (not a dirty diff).
        (self.wt_path / "feature.txt").write_text("orphan-only work\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "feat: reachable only from detached HEAD", cwd=self.wt_path)
        detached_sha = subprocess.run(
            [_GIT, "-C", str(self.wt_path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout.strip()
        _run_git("checkout", "-q", "--detach", "HEAD", cwd=self.wt_path)

        result = self._cleanup(self._make_worktree(), force=True)

        assert result.clean is True
        assert not self.wt_path.exists()
        # The recovery bundle exists and actually contains the detached commit.
        dirs = self._recovery_dirs()
        assert len(dirs) == 1, f"exactly one recovery dir expected, got {dirs}"
        bundle = dirs[0] / "branch.bundle"
        assert bundle.is_file(), "branch bundle missing — the detached commit was not captured"
        # The bundle's tip is the detached commit (restorable via `git fetch <bundle> HEAD`).
        list_heads = subprocess.run(
            [_GIT, "-C", str(self.repo_main), "bundle", "list-heads", str(bundle)],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout
        assert detached_sha in list_heads, f"bundle does not contain the detached commit {detached_sha}: {list_heads}"

    def test_force_detached_head_inconclusive_probe_keeps_worktree(self) -> None:
        """#1506 fail-closed survives the new HEAD probe path.

        Under force the recovery capture is the only safety net. When HEAD's
        commit object is unreadable, the ``git -C <wt_path> bundle HEAD`` capture
        fails AND the post-failure re-check (``git -C <wt_path> log HEAD --not
        --remotes``) errors → fails open to "might lose work" → teardown REFUSES
        and the worktree is kept on disk, never hard-deleted.
        """
        (self.wt_path / "feature.txt").write_text("at-risk detached work\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "feat: at-risk detached work", cwd=self.wt_path)
        _run_git("checkout", "-q", "--detach", "HEAD", cwd=self.wt_path)
        self._corrupt_head_commit_object()

        with pytest.raises(RuntimeError) as excinfo:
            self._cleanup(self._make_worktree(), force=True)

        message = str(excinfo.value)
        assert "recovery capture failed" in message
        assert "unrecoverable work" in message
        # The worktree is kept on disk — fail-closed, not hard-deleted.
        assert self.wt_path.exists(), "inconclusive capture under force must keep the worktree, not destroy it"
        assert self._recovery_dirs() == []


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
    def _tmp_workspace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.temp_root = tmp_path / "systmp"
        self.temp_root.mkdir()
        monkeypatch.setattr(
            "teatree.core.worktree_snapshot.tempfile.gettempdir",
            lambda: str(self.temp_root),
        )

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
            patch("teatree.core.cleanup.load_config") as mock_config,
            patch("teatree.core.cleanup.get_overlay") as mock_overlay,
            patch("teatree.core.cleanup._branch_pr_is_merged", return_value=False),
        ):
            mock_config.return_value.user.workspace_dir = self.workspace
            mock_overlay.return_value.get_cleanup_steps.return_value = []
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
