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

from teatree.core.models import Ticket, Worktree
from teatree.core.runners import WorktreeTeardown
from teatree.core.worktree.worktree_done import ReapOutcome
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

        def fake_reap(worktree: Worktree, *, workspace: Path, dry_run: bool, fsm_terminal: bool) -> ReapOutcome:
            # FSM teardown bypasses the FSM-ceremony liveness false positives via
            # fsm_terminal=True (#2243; #2773's respect_liveness=False equivalent).
            assert fsm_terminal is True, "FSM teardown must pass fsm_terminal=True"
            del workspace, dry_run
            cleaned.append(worktree.repo_path)
            worktree.delete()
            return ReapOutcome("wiped", f"Wiped '{worktree.branch}': Cleaned: {worktree.repo_path}")

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.teardown.reap_done_worktree", side_effect=fake_reap),
        ):
            result = WorktreeTeardown(ticket).run()

        assert result.ok is True
        assert sorted(cleaned) == ["repo-0", "repo-1"]
        assert ticket.worktrees.count() == 0

    def test_keeps_unproven_worktree_and_still_wipes_the_rest(self) -> None:
        """A kept (not-proven-redundant) worktree makes teardown ok=False, but the rest wipe."""
        ticket = self._ticket_with_worktrees(count=2)

        def fake_reap(worktree: Worktree, *, workspace: Path, dry_run: bool, fsm_terminal: bool) -> ReapOutcome:
            # FSM teardown bypasses the FSM-ceremony liveness false positives via
            # fsm_terminal=True (#2243; #2773's respect_liveness=False equivalent).
            assert fsm_terminal is True, "FSM teardown must pass fsm_terminal=True"
            del workspace, dry_run
            if worktree.repo_path == "repo-0":
                return ReapOutcome("kept", f"KEPT '{worktree.branch}': branch ahead of main — do not wipe")
            worktree.delete()
            return ReapOutcome("wiped", f"Wiped '{worktree.branch}': Cleaned: {worktree.repo_path}")

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.teardown.reap_done_worktree", side_effect=fake_reap),
        ):
            result = WorktreeTeardown(ticket).run()

        assert result.ok is False
        assert "repo-0" in result.detail
        assert "branch ahead of main" in result.detail
        # repo-1 wiped even though repo-0 was kept
        assert ticket.worktrees.filter(repo_path="repo-1").count() == 0

    def test_stranded_non_wiped_outcome_surfaces_as_failure(self) -> None:
        """A non-wiped, non-kept outcome (excluded/active/skipped) must surface, not read as success.

        B2: the teardown previously mapped only kept→ok=False / wiped→ok=True, so an
        EXCLUDED/ACTIVE/SKIPPED/WOULD-WIPE worktree the reaper left standing fell
        through to ``ok=True`` "tore down 0 worktree(s)" — success while tearing
        nothing down. Any surviving worktree on the FSM path is now ``ok=False``.
        """
        ticket = self._ticket_with_worktrees(count=1)

        def fake_reap(worktree: Worktree, *, workspace: Path, dry_run: bool, fsm_terminal: bool) -> ReapOutcome:
            del workspace, dry_run, fsm_terminal
            return ReapOutcome("excluded", f"EXCLUDED '{worktree.branch}': colleague-authored on a product repo")

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.teardown.reap_done_worktree", side_effect=fake_reap),
        ):
            result = WorktreeTeardown(ticket).run()

        assert result.ok is False, result.detail
        assert "EXCLUDED" in result.detail
        assert ticket.worktrees.count() == 1, "a stranded worktree is left intact, never silently dropped"


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

    def _teardown(self, ticket: Ticket) -> object:
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.cleanup.cleanup.clone_root", return_value=self.workspace),
            patch("teatree.core.cleanup.cleanup.get_overlay_for_worktree") as mock_overlay,
        ):
            mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
            return WorktreeTeardown(ticket).run()

    def test_refuses_to_remove_worktree_with_unpushed_commits(self) -> None:
        """The session-destroying scenario: branch has commits on NO remote."""
        ticket = self._ticket_with_worktree()
        self._commit_in_worktree("unpushed work")

        result = self._teardown(ticket)

        assert result.ok is False
        assert self.branch in result.detail
        assert "not provably on origin/main" in result.detail
        assert "salvage" in result.detail
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

    def test_no_extra_commits_proceeds(self) -> None:
        """A worktree with no commits beyond the pushed base is safe."""
        ticket = self._ticket_with_worktree()

        result = self._teardown(ticket)

        assert result.ok is True, result.detail
        assert not self.wt_path.exists()

    def test_proceeds_when_db_slug_drifted_from_the_real_clean_branch(self) -> None:
        """A drifted DB slug no longer hides the real (clean+pushed) worktree branch.

        The Worktree row names a slug git never heard of, but the on-disk
        worktree is on its real branch ``ac-myrepo-706-x`` (clean, nothing beyond
        the pushed base). The teardown seam resolves the EFFECTIVE branch/HEAD
        from the worktree dir rather than trusting the slug, so the data-loss
        probe runs against the real HEAD, finds nothing to lose, and teardown
        proceeds. Pre-fix the slug probe errored with "unknown revision" and the
        teardown refused with a cryptic message naming a non-existent branch.
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

        assert result.ok is True, result.detail
        assert "unknown revision" not in result.detail
        assert not self.wt_path.exists()
        assert not Worktree.objects.filter(branch="branch-git-never-heard-of").exists()
