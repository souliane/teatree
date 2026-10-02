"""``resolve_clear_changed_files`` resolves the INVOKING worktree's diff (#776, #1967).

The §17.4 CLEAR-side E2E gate must classify the diff of the branch the CLEAR is
acting on — not the ticket's earliest (often already-merged) worktree row. A
reused ticket spanning N workstreams records the invoking branch on
``extra['ship_invoking_branch']``; the resolver must share the canonical
:func:`resolve_ship_worktree` so the CLEAR side and the ship side classify the
same tree. The pre-#776 ``worktrees.first()`` returned the stale earliest row.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.management.commands._clear_preflight import resolve_clear_changed_files
from teatree.core.models import Ticket, Worktree


class TestResolveClearChangedFiles(TestCase):
    def test_none_ticket_returns_empty(self) -> None:
        assert resolve_clear_changed_files(None) == []

    def test_uses_invoking_worktree_not_earliest(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/i/76", overlay="t3-teatree")
        Worktree.objects.create(
            ticket=ticket,
            overlay="t3-teatree",
            repo_path="/tmp/stale",
            branch="ac/old-workstream",
            extra={"worktree_path": "/tmp/stale"},
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="t3-teatree",
            repo_path="/tmp/current",
            branch="ac/current-workstream",
            extra={"worktree_path": "/tmp/current"},
        )
        ticket.extra = {"ship_invoking_branch": "ac/current-workstream"}
        ticket.save(update_fields=["extra"])

        with patch("teatree.visual_qa.changed_files", side_effect=lambda repo: [repo]) as changed:
            result = resolve_clear_changed_files(ticket)

        changed.assert_called_once_with(repo="/tmp/current")
        assert result == ["/tmp/current"]

    def test_falls_back_to_dot_when_no_worktree(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/i/77", overlay="t3-teatree")
        with patch("teatree.visual_qa.changed_files", side_effect=lambda repo: [repo]) as changed:
            result = resolve_clear_changed_files(ticket)
        changed.assert_called_once_with(repo=".")
        assert result == ["."]

    def test_git_failure_fails_closed_to_empty(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/i/78", overlay="t3-teatree")
        Worktree.objects.create(
            ticket=ticket,
            overlay="t3-teatree",
            repo_path="/tmp/x",
            branch="b",
            extra={"worktree_path": "/tmp/x"},
        )
        with patch("teatree.visual_qa.changed_files", side_effect=RuntimeError("boom")):
            assert resolve_clear_changed_files(ticket) == []
