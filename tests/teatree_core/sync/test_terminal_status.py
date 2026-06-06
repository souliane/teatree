"""Merged/closed terminal-status tests (souliane/teatree#443 split of test_sync.py).

Covers apply_merged_status and apply_closed_status.
"""

from contextlib import AbstractContextManager
from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.backends.gitlab_sync_terminal import apply_closed_status, apply_merged_status
from teatree.core.e2e_workitem import record_run
from teatree.core.gates import dod_gate
from teatree.core.models import Ticket, Worktree
from teatree.types import SyncResult

_FRONTEND = "frontend"


def _patch_dod_overlay(frontend_repos: list[str]) -> AbstractContextManager[MagicMock]:
    """Patch the frontend-repo resolution seam the DoD gate delegates to."""
    return patch.object(dod_gate, "frontend_repos_for_overlay", return_value=list(frontend_repos))


class TestApplyMergedStatusAllMerged(TestCase):
    @patch("teatree.backends.gitlab_sync_terminal.cleanup_worktree")
    def test_advances_state_when_all_merged_no_discussions(self, mock_cleanup: MagicMock) -> None:
        """All MRs merged but none have discussions — state should still advance."""
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/1",
            state=Ticket.State.IN_REVIEW,
            extra={"prs": {"url1": {"title": "MR1"}, "url2": {"title": "MR2"}}},
        )
        result = SyncResult()
        apply_merged_status(ticket, {"url1", "url2"}, result)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_does_not_advance_when_some_unmerged(self) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/2",
            state=Ticket.State.IN_REVIEW,
            extra={"prs": {"url1": {"title": "MR1"}, "url2": {"title": "MR2"}}},
        )
        result = SyncResult()
        apply_merged_status(ticket, {"url1"}, result)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    @patch("teatree.backends.gitlab_sync_terminal.cleanup_worktree")
    def test_auto_cleans_worktrees_on_merge(self, mock_cleanup: MagicMock) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/3",
            state=Ticket.State.IN_REVIEW,
            extra={"prs": {"url1": {"title": "MR1"}}},
        )
        Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="org/repo",
            branch="fix-3",
        )
        result = SyncResult()
        apply_merged_status(ticket, {"url1"}, result)
        mock_cleanup.assert_called_once()
        assert result.worktrees_cleaned == 1

    @patch("teatree.backends.gitlab_sync_terminal.cleanup_worktree", side_effect=RuntimeError("cleanup failed"))
    def test_cleanup_failure_does_not_block_merge(self, mock_cleanup: MagicMock) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/4",
            state=Ticket.State.IN_REVIEW,
            extra={"prs": {"url1": {"title": "MR1"}}},
        )
        Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="org/repo",
            branch="fix-4",
        )
        result = SyncResult()
        apply_merged_status(ticket, {"url1"}, result)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        assert result.worktrees_cleaned == 0
        assert any("cleanup failed" in e for e in result.errors)
        # Error must carry the repo + branch so the dashboard can point at the stuck worktree.
        assert any("org/repo" in e and "fix-4" in e for e in result.errors)


class TestApplyClosedStatus(TestCase):
    """Closed-without-merge MRs: drop discussions, mark state=closed, never advance ticket FSM."""

    def test_marks_cached_state_closed(self) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/77",
            state=Ticket.State.IN_REVIEW,
            extra={
                "prs": {
                    "url1": {"title": "MR1", "state": "opened"},
                },
            },
        )
        result = SyncResult()
        apply_closed_status(ticket, {"url1"}, result)
        ticket.refresh_from_db()
        assert ticket.extra["prs"]["url1"]["state"] == "closed"
        assert ticket.state == Ticket.State.IN_REVIEW
        assert result.prs_closed == 1

    def test_does_not_change_ticket_state_when_all_closed(self) -> None:
        """Closing the only MR must NOT advance the ticket FSM (no FSM target for closed)."""
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/78",
            state=Ticket.State.IN_REVIEW,
            extra={"prs": {"url1": {"title": "MR1", "state": "opened"}}},
        )
        result = SyncResult()
        apply_closed_status(ticket, {"url1"}, result)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_drops_discussions_from_closed_mr(self) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/79",
            state=Ticket.State.IN_REVIEW,
            extra={
                "prs": {
                    "url1": {
                        "title": "MR1",
                        "state": "opened",
                        "discussions": [{"status": "needs_reply", "detail": "fix"}],
                    },
                },
            },
        )
        result = SyncResult()
        apply_closed_status(ticket, {"url1"}, result)
        ticket.refresh_from_db()
        assert "discussions" not in ticket.extra["prs"]["url1"]

    def test_noop_when_prs_empty(self) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/81",
            state=Ticket.State.IN_REVIEW,
            extra={"prs": {}},
        )
        result = SyncResult()
        apply_closed_status(ticket, {"url1"}, result)
        ticket.refresh_from_db()
        assert result.prs_closed == 0

    def test_noop_when_prs_not_dict(self) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/82",
            state=Ticket.State.IN_REVIEW,
            extra={"prs": "bad"},
        )
        result = SyncResult()
        apply_closed_status(ticket, {"url1"}, result)
        assert result.prs_closed == 0

    def test_skips_non_dict_pr_entry(self) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/83",
            state=Ticket.State.IN_REVIEW,
            extra={"prs": {"url1": "not-a-dict", "url2": {"title": "MR2", "state": "opened"}}},
        )
        result = SyncResult()
        apply_closed_status(ticket, {"url1", "url2"}, result)
        ticket.refresh_from_db()
        assert result.prs_closed == 1

    def test_does_not_clean_worktrees(self) -> None:
        """Closed MRs leave worktrees alone — user may reopen with new MR."""
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/80",
            state=Ticket.State.IN_REVIEW,
            extra={"prs": {"url1": {"title": "MR1", "state": "opened"}}},
        )
        Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="org/repo",
            branch="fix-80",
        )
        result = SyncResult()
        with patch("teatree.backends.gitlab_sync_terminal.cleanup_worktree") as mock_cleanup:
            apply_closed_status(ticket, {"url1"}, result)
        mock_cleanup.assert_not_called()
        assert result.worktrees_cleaned == 0


class TestMergedTerminalDodGate(TestCase):
    """A genuinely-merged PR is terminal reality: keep MERGED, but audit a DoD gap (#1426).

    Demoting a merged-PR ticket to STARTED would make it contradict reality,
    so the sync follows the merge and instead records a durable
    ``dod_e2e_violation`` marker when the DoD local-E2E gate was unmet.
    """

    @patch("teatree.backends.gitlab_sync_terminal.cleanup_worktree")
    def test_ui_visible_no_e2e_keeps_merged_but_records_violation(self, mock_cleanup: MagicMock) -> None:
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://gitlab.com/org/repo/-/issues/501",
            state=Ticket.State.IN_REVIEW,
            repos=[_FRONTEND],
            extra={"prs": {"url1": {"title": "MR1"}}},
        )
        result = SyncResult()
        with _patch_dod_overlay([_FRONTEND]):
            apply_merged_status(ticket, {"url1"}, result)

        ticket.refresh_from_db()
        # Terminal reality is kept (not demoted to STARTED).
        assert ticket.state == Ticket.State.MERGED
        # The unmet DoD is recorded for audit rather than silently bypassed.
        assert ticket.extra["dod_e2e_violation"]["state"] == Ticket.State.MERGED

    @patch("teatree.backends.gitlab_sync_terminal.cleanup_worktree")
    def test_ui_visible_with_green_e2e_keeps_merged_no_violation(self, mock_cleanup: MagicMock) -> None:
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://gitlab.com/org/repo/-/issues/502",
            state=Ticket.State.IN_REVIEW,
            repos=[_FRONTEND],
            extra={"prs": {"url1": {"title": "MR1"}}},
        )
        record_run(ticket, result="green", per_repo_shas={_FRONTEND: "sha"}, env="local")
        result = SyncResult()
        with _patch_dod_overlay([_FRONTEND]):
            apply_merged_status(ticket, {"url1"}, result)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        assert "dod_e2e_violation" not in ticket.extra

    @patch("teatree.backends.gitlab_sync_terminal.cleanup_worktree")
    def test_backend_only_keeps_merged_no_violation(self, mock_cleanup: MagicMock) -> None:
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://gitlab.com/org/repo/-/issues/503",
            state=Ticket.State.IN_REVIEW,
            repos=["backend"],
            extra={"prs": {"url1": {"title": "MR1"}}},
        )
        result = SyncResult()
        with _patch_dod_overlay([_FRONTEND]):
            apply_merged_status(ticket, {"url1"}, result)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        assert "dod_e2e_violation" not in ticket.extra
