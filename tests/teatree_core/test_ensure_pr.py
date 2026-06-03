"""Tests for the ensure-pr helpers (mirrors ``_ensure_pr``).

Split out of ``test_pr_command`` alongside the ``_ensure_pr`` module
extraction: test files mirror the production module path. The behavioural
``ensure-pr`` command tests (PUSHED_ORPHAN / pre-push-deadlock deferral)
stay in ``test_pr_command`` because they drive ``call_command("pr",
"ensure-pr")`` end to end.
"""

from django.test import TestCase

from teatree.core.management.commands._ensure_pr import _ticket_extra_for_branch
from teatree.core.models import Ticket, Worktree


class TestTicketExtraForBranch(TestCase):
    """Resolve the owning ticket's ``extra`` from the orphan-branch name (#873).

    Lets the pre-push ``ensure-pr`` fallback honor the explicit
    ``more_prs_coming`` opt-out even though it has no ticket handle.
    """

    def test_returns_none_when_no_worktree_for_branch(self) -> None:
        assert _ticket_extra_for_branch("no-such-branch") is None

    def test_returns_ticket_extra_for_known_branch(self) -> None:
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://github.com/souliane/teatree/issues/873",
            extra={"more_prs_coming": True},
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/repo873",
            branch="fix/873-x",
            extra={"worktree_path": "/tmp/repo873"},
        )
        assert _ticket_extra_for_branch("fix/873-x") == {"more_prs_coming": True}

    def test_returns_latest_worktree_when_branch_reused(self) -> None:
        old = Ticket.objects.create(overlay="test", issue_url="https://x/1", extra={"more_prs_coming": True})
        new = Ticket.objects.create(overlay="test", issue_url="https://x/2", extra={})
        Worktree.objects.create(ticket=old, overlay="test", repo_path="/tmp/a", branch="shared")
        Worktree.objects.create(ticket=new, overlay="test", repo_path="/tmp/b", branch="shared")
        assert _ticket_extra_for_branch("shared") == {}
