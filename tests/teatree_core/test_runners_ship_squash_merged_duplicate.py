"""The #776 duplicate-PR refusal must see a squash-merge, not just an ancestor (#4070).

``ShipExecutor`` refused to re-open a PR for a branch "already merged into base" using
``git branch --merged`` — an ancestor test. A squash-merge rewrites the branch's commits
into a new sha on the default branch, so the branch is NOT an ancestor and the guard
stayed silent: the ticket got a second PR for work that had already shipped.

Real git under ``tmp_path``, because the condition IS the sha rewrite. The ancestor case
is pinned alongside it, so the swap can only ever widen the refusal.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.runners import ShipExecutor
from tests._git_repo import make_git_repo, run_git
from tests.teatree_core.conftest import CommandOverlay

_MOCK_OVERLAY = {"test": CommandOverlay()}


def _commit(repo: Path, name: str, body: str) -> None:
    (repo / name).write_text(body)
    run_git(repo, "add", name)
    run_git(repo, "commit", "-q", "-m", f"add {name}")


def _clone_with_origin(root: Path) -> Path:
    origin = make_git_repo(root / "origin", bare=True)
    work = make_git_repo(root / "work")
    run_git(work, "config", "user.name", "Test")
    run_git(work, "config", "user.email", "test@example.com")
    _commit(work, "README.md", "base\n")
    run_git(work, "remote", "add", "origin", str(origin))
    run_git(work, "push", "-q", "origin", "main")
    run_git(work, "fetch", "-q", "origin")
    return work


@pytest.mark.usefixtures("tmp_path")
class TestShipRefusesAnAlreadyLandedBranch(TestCase):
    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def _ticket_on(self, repo: Path, branch: str) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/4070")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path=str(repo),
            branch=branch,
            extra={"worktree_path": str(repo)},
        )
        return ticket

    def _ship(self, ticket: Ticket) -> tuple[object, MagicMock, MagicMock]:
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "https://example.com/mr/dup"}
        host.current_user.return_value = "souliane"
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.push_branch") as push,
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat: x", "body")),
        ):
            result = ShipExecutor(ticket).run()
        return result, push, host

    def test_a_squash_merged_branch_is_refused(self) -> None:
        repo = _clone_with_origin(self.root)
        run_git(repo, "checkout", "-q", "-b", "feature")
        _commit(repo, "one.py", "ONE\n")
        run_git(repo, "checkout", "-q", "main")
        run_git(repo, "merge", "-q", "--squash", "feature")
        run_git(repo, "commit", "-q", "-m", "feat: one (#1)")
        run_git(repo, "push", "-q", "origin", "main")
        run_git(repo, "fetch", "-q", "origin")

        result, push, host = self._ship(self._ticket_on(repo, "feature"))

        assert result.ok is False
        assert "merged" in result.detail.lower()
        push.assert_not_called()
        host.create_pr.assert_not_called()

    def test_a_plain_merged_branch_is_still_refused(self) -> None:
        repo = _clone_with_origin(self.root)
        run_git(repo, "checkout", "-q", "-b", "feature")
        _commit(repo, "one.py", "ONE\n")
        run_git(repo, "checkout", "-q", "main")
        run_git(repo, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
        run_git(repo, "push", "-q", "origin", "main")
        run_git(repo, "fetch", "-q", "origin")

        result, push, _host = self._ship(self._ticket_on(repo, "feature"))

        assert result.ok is False
        push.assert_not_called()

    def test_an_unmerged_branch_still_ships(self) -> None:
        repo = _clone_with_origin(self.root)
        run_git(repo, "checkout", "-q", "-b", "feature")
        _commit(repo, "one.py", "ONE\n")

        result, push, _host = self._ship(self._ticket_on(repo, "feature"))

        assert result.ok is True
        push.assert_called_once()
