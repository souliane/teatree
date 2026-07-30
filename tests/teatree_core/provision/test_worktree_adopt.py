"""Follow-up-PR worktree adoption seam (#3327).

Adoption attaches an existing on-disk worktree to a terminal ticket as a new
``Worktree`` row so ``pr create`` can open PR-B after PR-A merged and its row
was torn down. The guardrails are load-bearing — adoption must never become a
way to re-ship merged work — so the dup-row and path-claimed refusals, and the
reopen edge's terminal scoping, are pinned here.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.provision.worktree_adopt import (
    WorktreeAdoptError,
    adopt_worktree_for_ticket,
    reopen_ticket_for_followup,
)

_BRANCH = "teatree.core.provision.worktree_adopt.git.current_branch"


class TestAdoptWorktreeForTicket(TestCase):
    @pytest.fixture(autouse=True)
    def _inject(self, tmp_path: Path) -> None:
        self._tmp = tmp_path

    def _make_worktree(self, name: str = "backend") -> Path:
        wt = self._tmp / name
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /some/.git/worktrees/backend\n")
        return wt

    def test_creates_row_for_terminal_ticket(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.MERGED)
        wt = self._make_worktree()

        with patch(_BRANCH, return_value="4321-followup"):
            row = adopt_worktree_for_ticket(ticket, cwd=str(wt))

        assert row.ticket_id == ticket.pk
        assert row.overlay == "test"
        assert row.repo_path == "backend"
        assert row.branch == "4321-followup"
        assert row.extra["worktree_path"] == str(wt.resolve())

    def test_refuses_non_git_worktree_path(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.MERGED)
        plain = self._tmp / "plain"
        plain.mkdir()

        with (
            patch(_BRANCH, return_value="4321-followup"),
            pytest.raises(WorktreeAdoptError, match="not a git worktree"),
        ):
            adopt_worktree_for_ticket(ticket, cwd=str(plain))
        assert not Worktree.objects.exists()

    def test_refuses_main_clone_dot_git_directory(self) -> None:
        # A main clone keeps .git as a DIRECTORY — the #752 main-clone refusal.
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.MERGED)
        clone = self._tmp / "clone"
        (clone / ".git").mkdir(parents=True)

        with (
            patch(_BRANCH, return_value="4321-followup"),
            pytest.raises(WorktreeAdoptError, match="not a git worktree"),
        ):
            adopt_worktree_for_ticket(ticket, cwd=str(clone))

    def test_refuses_non_feature_branch(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.MERGED)
        wt = self._make_worktree()

        with patch(_BRANCH, return_value="main"), pytest.raises(WorktreeAdoptError, match="feature branch"):
            adopt_worktree_for_ticket(ticket, cwd=str(wt))

    def test_refuses_duplicate_ticket_repo_branch_row(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.MERGED)
        wt = self._make_worktree()
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="backend",
            branch="4321-followup",
            extra={"worktree_path": "/somewhere/else"},
        )

        with (
            patch(_BRANCH, return_value="4321-followup"),
            pytest.raises(WorktreeAdoptError, match="refusing to adopt a duplicate"),
        ):
            adopt_worktree_for_ticket(ticket, cwd=str(wt))

    def test_refuses_path_claimed_by_another_row(self) -> None:
        owner = Ticket.objects.create(overlay="test", state=Ticket.State.MERGED)
        adopter = Ticket.objects.create(overlay="test", state=Ticket.State.MERGED)
        wt = self._make_worktree()
        Worktree.objects.create(
            ticket=owner,
            overlay="test",
            repo_path="backend",
            branch="4321-followup",
            extra={"worktree_path": str(wt.resolve())},
        )

        with patch(_BRANCH, return_value="9999-other"), pytest.raises(WorktreeAdoptError, match="already records"):
            adopt_worktree_for_ticket(adopter, cwd=str(wt))


class TestReopenTicketForFollowup(TestCase):
    def test_merged_reopens_to_reviewed(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.MERGED)

        reopen_ticket_for_followup(ticket)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED

    def test_delivered_reopens_to_reviewed(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.DELIVERED)

        reopen_ticket_for_followup(ticket)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED

    def test_shipped_is_left_untouched(self) -> None:
        # SHIPPED is already a legal ship() source — the edge must not fire and
        # drag it backward to REVIEWED.
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.SHIPPED)

        reopen_ticket_for_followup(ticket)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED

    def test_ignored_is_left_untouched(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IGNORED)

        reopen_ticket_for_followup(ticket)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IGNORED
