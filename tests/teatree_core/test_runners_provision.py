"""Tests for WorktreeProvisioner — composed runner for the start transition.

Stage 3 of #140: ``Ticket.start()`` becomes a thin transition that enqueues
the heavy I/O (git worktree creation, Worktree DB rows) onto a ``@task``
worker. The worker runs ``WorktreeProvisioner`` and on success schedules
the coding task.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import reset_overlay_cache
from teatree.core.runners import WorktreeProvisioner
from tests.teatree_core.conftest import CommandOverlay


@pytest.fixture(autouse=True)
def _clear_overlay_cache() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()


_MOCK_OVERLAY = {"test": CommandOverlay()}


class TestWorktreeProvisioner(TestCase):
    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()

    def _scoped_ticket(self, repos: list[str], *, branch: str = "ac-repo-77-x") -> Ticket:
        return Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/77",
            repos=repos,
            extra={"branch": branch, "description": "x"},
        )

    def _patch_workspace_dir(self) -> Any:
        return patch("teatree.core.runners.provision._workspace_dir", return_value=self.workspace)

    def test_returns_failure_when_no_repos(self) -> None:
        ticket = self._scoped_ticket(repos=[])

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            self._patch_workspace_dir(),
        ):
            result = WorktreeProvisioner(ticket).run()

        assert result.ok is False
        assert "no repos" in result.detail.lower()

    def test_creates_worktree_rows_and_git_worktrees(self) -> None:
        repo_dir = self.workspace / "repo-a"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        ticket = self._scoped_ticket(repos=["repo-a"], branch="ac-repo-a-77-x")

        created_paths: list[str] = []

        def fake_worktree_add(repo: str, path: str, branch: str, *, create_branch: bool = True) -> bool:
            del repo, branch, create_branch
            Path(path).mkdir(parents=True, exist_ok=True)
            created_paths.append(path)
            return True

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            self._patch_workspace_dir(),
            patch("teatree.core.runners.provision.git.worktree_add", side_effect=fake_worktree_add),
            patch("teatree.core.runners.provision.git.pull_ff_only", return_value=True),
        ):
            result = WorktreeProvisioner(ticket).run()

        assert result.ok is True
        wt_path = self.workspace / "ac-repo-a-77-x" / "repo-a"
        assert str(wt_path) in created_paths

        worktrees = list(Worktree.objects.filter(ticket=ticket))
        assert len(worktrees) == 1
        assert worktrees[0].repo_path == "repo-a"
        assert worktrees[0].branch == "ac-repo-a-77-x"
        assert (worktrees[0].extra or {}).get("worktree_path") == str(wt_path)

    def test_idempotent_when_worktree_already_exists(self) -> None:
        repo_dir = self.workspace / "repo-a"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        ticket = self._scoped_ticket(repos=["repo-a"], branch="ac-repo-a-77-x")
        ticket_dir = self.workspace / "ac-repo-a-77-x"
        ticket_dir.mkdir()
        existing_path = ticket_dir / "repo-a"
        existing_path.mkdir()
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="repo-a",
            branch="ac-repo-a-77-x",
            extra={"worktree_path": str(existing_path)},
        )

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            self._patch_workspace_dir(),
            patch("teatree.core.runners.provision.git.worktree_add") as worktree_add,
        ):
            result = WorktreeProvisioner(ticket).run()

        assert result.ok is True
        worktree_add.assert_not_called()
        assert Worktree.objects.filter(ticket=ticket).count() == 1

    def test_returns_failure_when_worktree_add_fails(self) -> None:
        repo_dir = self.workspace / "repo-a"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        ticket = self._scoped_ticket(repos=["repo-a"], branch="ac-repo-a-77-x")

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            self._patch_workspace_dir(),
            patch("teatree.core.runners.provision.git.worktree_add", return_value=False),
            patch("teatree.core.runners.provision.git.pull_ff_only", return_value=True),
        ):
            result = WorktreeProvisioner(ticket).run()

        assert result.ok is False
        assert "repo-a" in result.detail
        assert Worktree.objects.filter(ticket=ticket, repo_path="repo-a").count() == 0

    def test_returns_failure_when_no_clone_found_anywhere(self) -> None:
        not_a_repo = self.workspace / "no-git"
        not_a_repo.mkdir()
        ticket = self._scoped_ticket(repos=["no-git"], branch="ac-no-git-77-x")

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            self._patch_workspace_dir(),
            patch("teatree.core.runners.provision.git.worktree_add") as worktree_add,
        ):
            result = WorktreeProvisioner(ticket).run()

        assert result.ok is False
        assert "no-git" in result.detail
        worktree_add.assert_not_called()
        assert Worktree.objects.filter(ticket=ticket, repo_path="no-git").count() == 0

    def test_finds_clone_under_namespaced_subdir(self) -> None:
        namespaced = self.workspace / "souliane" / "teatree"
        namespaced.mkdir(parents=True)
        (namespaced / ".git").mkdir()
        ticket = self._scoped_ticket(repos=["teatree"], branch="ac-teatree-491-x")

        captured: dict[str, str] = {}

        def fake_worktree_add(repo: str, path: str, branch: str, *, create_branch: bool = True) -> bool:
            del branch, create_branch
            captured["source"] = repo
            captured["dest"] = path
            Path(path).mkdir(parents=True, exist_ok=True)
            return True

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            self._patch_workspace_dir(),
            patch("teatree.core.runners.provision.git.worktree_add", side_effect=fake_worktree_add),
            patch("teatree.core.runners.provision.git.pull_ff_only", return_value=True),
        ):
            result = WorktreeProvisioner(ticket).run()

        assert result.ok is True
        assert captured["source"] == str(namespaced)
        assert captured["dest"] == str(self.workspace / "ac-teatree-491-x" / "teatree")
        wt = Worktree.objects.get(ticket=ticket, repo_path="teatree")
        assert (wt.extra or {}).get("worktree_path") == captured["dest"]
        assert (wt.extra or {}).get("clone_path") == str(namespaced)

    def test_warns_when_multiple_clones_match_basename(self) -> None:
        first = self.workspace / "alpha" / "teatree"
        second = self.workspace / "zeta" / "teatree"
        for clone in (first, second):
            clone.mkdir(parents=True)
            (clone / ".git").mkdir()
        ticket = self._scoped_ticket(repos=["teatree"], branch="ac-teatree-491-multi")

        captured: dict[str, str] = {}

        def fake_worktree_add(repo: str, path: str, branch: str, *, create_branch: bool = True) -> bool:
            del branch, create_branch
            captured["source"] = repo
            Path(path).mkdir(parents=True, exist_ok=True)
            return True

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            self._patch_workspace_dir(),
            patch("teatree.core.runners.provision.git.worktree_add", side_effect=fake_worktree_add),
            patch("teatree.core.runners.provision.git.pull_ff_only", return_value=True),
            self.assertLogs("teatree.core.clone_paths", level="WARNING") as cm,
        ):
            result = WorktreeProvisioner(ticket).run()

        assert result.ok is True
        assert captured["source"] == str(first)
        assert any("Multiple clones match" in msg for msg in cm.output)

    def test_returns_failure_when_branch_missing_from_extra(self) -> None:
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/79",
            repos=["repo-a"],
        )

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            self._patch_workspace_dir(),
        ):
            result = WorktreeProvisioner(ticket).run()

        assert result.ok is False
        assert "branch" in result.detail.lower()
