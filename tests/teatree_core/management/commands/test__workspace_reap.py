"""``reap_one_worktree`` — the funnel guard that never reaps live work.

Both clean-all worktree loops (the CREATED-state loop and the squash-merged
reaper) funnel through :func:`reap_one_worktree`. It consults the shared
liveness predicate (``idle_stack.ticket_is_busy``) FIRST: a worktree whose
ticket has a live :class:`Session` or an active/claimed :class:`Task` is live
work and is KEPT, never handed to the destructive teardown (#291/#2243).
"""

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.management.commands import _workspace_reap as reap
from teatree.core.models import Session, Task, Ticket, Worktree

_REAP = "teatree.core.management.commands._workspace_reap"


class TestReapOneWorktreeLivenessGuard(TestCase):
    def _make_worktree(self, *, branch: str = "feature") -> Worktree:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/2243")
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="org/repo",
            branch=branch,
            state=Worktree.State.PROVISIONED,
            extra={"worktree_path": "/tmp/does-not-matter"},
        )

    def test_live_session_keeps_worktree_without_teardown(self) -> None:
        """A live session short-circuits before the destructive teardown is reached."""
        worktree = self._make_worktree()
        Session.objects.create(ticket=worktree.ticket, overlay="test")  # live: ended_at is null

        with patch(f"{_REAP}.cleanup_worktree") as cleanup:
            line = reap.reap_one_worktree(worktree, interactive=False)

        cleanup.assert_not_called()
        assert Worktree.objects.filter(pk=worktree.pk).exists(), "DATA LOSS: live worktree reaped mid-task"
        assert "SKIPPED" in line
        assert "live work" in line

    def test_claimed_task_keeps_worktree_without_teardown(self) -> None:
        """An active task on an ended session still marks the worktree live."""
        worktree = self._make_worktree(branch="task-feature")
        session = Session.objects.create(ticket=worktree.ticket, overlay="test")
        session.ended_at = timezone.now()
        session.save(update_fields=["ended_at"])
        Task.objects.create(ticket=worktree.ticket, session=session, status=Task.Status.CLAIMED)

        with patch(f"{_REAP}.cleanup_worktree") as cleanup:
            line = reap.reap_one_worktree(worktree, interactive=False)

        cleanup.assert_not_called()
        assert "SKIPPED" in line
        assert "live work" in line

    def test_idle_ticket_is_reaped(self) -> None:
        """A worktree with no live work is handed to teardown as before (safe-reap preserved)."""
        worktree = self._make_worktree(branch="idle-feature")

        with patch(f"{_REAP}.cleanup_worktree", return_value="Cleaned: org/repo (idle-feature)") as cleanup:
            line = reap.reap_one_worktree(worktree, interactive=False)

        cleanup.assert_called_once()
        assert "Cleaned" in line
