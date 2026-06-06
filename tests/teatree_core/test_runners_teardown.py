"""Tests for WorktreeTeardown — composed runner for the mark_merged transition.

Stage 5 of #140: ``Ticket.mark_merged()`` becomes a thin transition that
enqueues teardown I/O (worktree removal, branch deletion, DB drop) onto a
``@task`` worker. The worker invokes ``WorktreeTeardown`` and on success
the ticket is ready for ``retrospect()``.
"""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.cleanup import CleanupResult
from teatree.core.models import Ticket, Worktree
from teatree.core.runners import WorktreeTeardown
from tests.teatree_core.conftest import CommandOverlay

_GIT = shutil.which("git") or "git"


_MOCK_OVERLAY = {"test": CommandOverlay()}


class TestWorktreeTeardown(TestCase):
    def _ticket_with_worktrees(self, count: int = 2) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/77")
        for i in range(count):
            Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path=f"repo-{i}",
                branch="feat-x",
                extra={"worktree_path": f"/tmp/wt-{i}"},
            )
        return ticket

    def test_returns_success_when_no_worktrees(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/78")

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = WorktreeTeardown(ticket).run()

        assert result.ok is True
        assert "no worktrees" in result.detail.lower()

    def test_cleans_each_worktree_and_returns_summary(self) -> None:
        ticket = self._ticket_with_worktrees(count=2)

        cleaned: list[str] = []

        def fake_cleanup(worktree: Worktree, *, force: bool = False, strict_hygiene: bool = True) -> CleanupResult:
            del force, strict_hygiene
            label = f"Cleaned: {worktree.repo_path}"
            cleaned.append(worktree.repo_path)
            worktree.delete()
            return CleanupResult(label=label)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.teardown.cleanup_worktree", side_effect=fake_cleanup),
        ):
            result = WorktreeTeardown(ticket).run()

        assert result.ok is True
        assert sorted(cleaned) == ["repo-0", "repo-1"]
        assert ticket.worktrees.count() == 0

    def test_continues_on_individual_failure_and_reports_errors(self) -> None:
        ticket = self._ticket_with_worktrees(count=2)

        def fake_cleanup(worktree: Worktree, *, force: bool = False, strict_hygiene: bool = True) -> CleanupResult:
            del force, strict_hygiene
            if worktree.repo_path == "repo-0":
                msg = "branch ahead of main"
                raise RuntimeError(msg)
            worktree.delete()
            return CleanupResult(label=f"Cleaned: {worktree.repo_path}")

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.teardown.cleanup_worktree", side_effect=fake_cleanup),
        ):
            result = WorktreeTeardown(ticket).run()

        assert result.ok is False
        assert "repo-0" in result.detail
        assert "branch ahead of main" in result.detail
        # repo-1 cleaned even though repo-0 raised
        assert ticket.worktrees.filter(repo_path="repo-1").count() == 0


def _run_git(*args: str, cwd: Path) -> None:
    subprocess.run([_GIT, "-C", str(cwd), *args], check=True, capture_output=True)


class TestWorktreeTeardownUnpushedGuard(TestCase):
    """#706 — the automated teardown path must NOT destroy unpushed work.

    ``execute_teardown`` (django-task) → ``WorktreeTeardown`` →
    ``cleanup_worktree``. The FSM can read MERGED while the branch was never
    actually pushed (async ship never drained). This exercises the *real*
    git path that physically deleted two worktree dirs in the wild, with a
    bare remote standing in for ``origin``.
    """

    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        # Bare repo acts as the shared remote (origin).
        self.remote = tmp_path / "remote.git"
        self.remote.mkdir()
        _run_git("init", "-q", "--bare", "-b", "main", cwd=self.remote)

        self.repo_main = self.workspace / "myrepo"
        self.repo_main.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.repo_main)
        _run_git("config", "user.name", "t", cwd=self.repo_main)
        _run_git("remote", "add", "origin", str(self.remote), cwd=self.repo_main)
        _run_git("commit", "--allow-empty", "-q", "-m", "initial", cwd=self.repo_main)
        _run_git("push", "-q", "origin", "main", cwd=self.repo_main)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)

        self.branch = "ac-myrepo-706-x"
        self.wt_path = self.workspace / self.branch / "myrepo"
        _run_git("worktree", "add", "-q", "-b", self.branch, str(self.wt_path), cwd=self.repo_main)

    def _commit_in_worktree(self, message: str) -> None:
        (self.wt_path / "file.txt").write_text(message, encoding="utf-8")
        _run_git("add", "file.txt", cwd=self.wt_path)
        _run_git("config", "user.email", "t@t", cwd=self.wt_path)
        _run_git("config", "user.name", "t", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", message, cwd=self.wt_path)

    def _ticket_with_worktree(self) -> Ticket:
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/706",
            state=Ticket.State.MERGED,
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="myrepo",
            branch=self.branch,
            extra={"worktree_path": str(self.wt_path)},
        )
        return ticket

    def _teardown(self, ticket: Ticket, **kwargs: bool) -> object:
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.cleanup.load_config") as mock_config,
            patch("teatree.core.cleanup.get_overlay") as mock_overlay,
        ):
            mock_config.return_value.user.workspace_dir = self.workspace
            mock_overlay.return_value.get_cleanup_steps.return_value = []
            return WorktreeTeardown(ticket, **kwargs).run()

    def test_refuses_to_remove_worktree_with_unpushed_commits(self) -> None:
        """The session-destroying scenario: branch has commits on NO remote."""
        ticket = self._ticket_with_worktree()
        self._commit_in_worktree("unpushed work")

        result = self._teardown(ticket)

        assert result.ok is False
        assert self.branch in result.detail
        assert "on NO remote" in result.detail
        assert "data loss" in result.detail
        assert self.wt_path.exists(), "worktree with unpushed commits was destroyed"
        assert Worktree.objects.filter(branch=self.branch).exists()

    def test_proceeds_when_branch_fully_pushed(self) -> None:
        """A branch whose HEAD is on a remote ref is safe to tear down."""
        ticket = self._ticket_with_worktree()
        self._commit_in_worktree("pushed work")
        _run_git("push", "-q", "origin", self.branch, cwd=self.wt_path)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)

        result = self._teardown(ticket)

        assert result.ok is True, result.detail
        assert not self.wt_path.exists()

    def test_force_overrides_the_guard(self) -> None:
        """An explicit force escape hatch tears down even unpushed work."""
        ticket = self._ticket_with_worktree()
        self._commit_in_worktree("unpushed but force")

        result = self._teardown(ticket, force=True)

        assert result.ok is True, result.detail
        assert not self.wt_path.exists()

    def test_no_extra_commits_proceeds(self) -> None:
        """A worktree with no commits beyond the pushed base is safe."""
        ticket = self._ticket_with_worktree()

        result = self._teardown(ticket)

        assert result.ok is True, result.detail
        assert not self.wt_path.exists()

    def test_fail_closed_when_branch_unknown_to_git(self) -> None:
        """#706 fail-closed when the data-loss probe cannot run.

        Here the Worktree row names a branch git doesn't know, so the probe
        errors. Teardown must REFUSE — we cannot prove the commits are pushed,
        so we must not destroy the worktree.
        """
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/706",
            state=Ticket.State.MERGED,
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="myrepo",
            branch="branch-git-never-heard-of",
            extra={"worktree_path": str(self.wt_path)},
        )

        result = self._teardown(ticket)

        assert result.ok is False
        assert self.wt_path.exists(), "worktree destroyed despite an inconclusive data-loss probe"
        assert Worktree.objects.filter(branch="branch-git-never-heard-of").exists()

    def test_force_with_inconclusive_probe_and_capture_failure_keeps_worktree(self) -> None:
        """#1506 — force skips the *guard* but the recovery capture still protects.

        Force bypasses the pre-remove data-loss guard, but the recovery capture
        becomes the only safety net. When the branch is unknown to git the
        capture (a ``git bundle`` of that branch) fails, and the post-failure
        re-check cannot prove there is nothing to lose — so it fails closed and
        the worktree is kept rather than hard-deleted. (Pre-#1506 this forced
        teardown destroyed the worktree on a capture failure.)
        """
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/706",
            state=Ticket.State.MERGED,
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="myrepo",
            branch="branch-git-never-heard-of",
            extra={"worktree_path": str(self.wt_path)},
        )

        result = self._teardown(ticket, force=True)

        assert result.ok is False, result.detail
        assert self.wt_path.exists(), "inconclusive capture must fail closed, not destroy the worktree"
