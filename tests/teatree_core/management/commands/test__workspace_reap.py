"""``reap_one_worktree`` — translates the funnel liveness keep into a clean line.

The liveness guard lives in :func:`teatree.core.cleanup.cleanup_worktree` (the
single seam every teardown caller routes through). ``reap_one_worktree`` — the
funnel both clean-all worktree loops use (the CREATED-state loop and the
squash-merged reaper) — catches :class:`WorktreeBusyError` and reports a clean
KEEP line, distinct from the unsynced-work ``RuntimeError`` path that offers
push/abandon (live work is not unpushed work).
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.cleanup import CleanupResult, WorktreeBusyError
from teatree.core.management.commands import _workspace_reap as reap
from teatree.core.models import Session, Ticket, Worktree

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

    def test_live_work_keeps_worktree(self) -> None:
        """A live worktree raises WorktreeBusyError in the funnel; the row is KEPT.

        Uses the real ``cleanup_worktree`` whose liveness guard fires FIRST (before
        any path/overlay/git resolution), so a live session short-circuits without
        touching the worktree.
        """
        worktree = self._make_worktree()
        Session.objects.create(ticket=worktree.ticket, overlay="test")  # live: ended_at is null

        line = reap.reap_one_worktree(worktree, interactive=False)

        assert Worktree.objects.filter(pk=worktree.pk).exists(), "DATA LOSS: live worktree reaped mid-task"
        assert "SKIPPED" in line
        assert "live work" in line

    def test_busy_error_reports_keep_not_push_abandon(self) -> None:
        """A WorktreeBusyError is translated to a clean SKIPPED line, never the resolve path."""
        worktree = self._make_worktree(branch="busy")
        busy = WorktreeBusyError("org/repo (busy): kept — ticket has a live session or active/claimed task (live work)")

        with (
            patch(f"{_REAP}.cleanup_worktree", side_effect=busy),
            patch(f"{_REAP}.resolve_unsynced_worktree") as resolve,
        ):
            line = reap.reap_one_worktree(worktree, interactive=True)

        resolve.assert_not_called()
        assert "SKIPPED" in line
        assert "live work" in line

    def test_unsynced_runtimeerror_routes_to_resolve(self) -> None:
        """A non-liveness RuntimeError still routes to the unsynced push/abandon resolver."""
        worktree = self._make_worktree(branch="unsynced")
        exc = RuntimeError("2 commit(s) on NO remote")

        with (
            patch(f"{_REAP}.cleanup_worktree", side_effect=exc),
            patch(f"{_REAP}.resolve_unsynced_worktree", return_value="Skipped: unsynced") as resolve,
        ):
            line = reap.reap_one_worktree(worktree, interactive=False)

        resolve.assert_called_once()
        assert line == "Skipped: unsynced"

    def test_idle_ticket_is_reaped(self) -> None:
        """A worktree with no live work is handed to teardown as before (safe-reap preserved)."""
        worktree = self._make_worktree(branch="idle-feature")
        result = CleanupResult(label="Cleaned: org/repo (idle-feature)")

        with patch(f"{_REAP}.cleanup_worktree", return_value=result) as cleanup:
            line = reap.reap_one_worktree(worktree, interactive=False)

        cleanup.assert_called_once()
        assert "Cleaned" in line
