"""``warn_orphans`` scopes its scan to in-flight worktrees (#15, rework of #4814).

The warning runs synchronously on every ``workspace ticket`` call and each
classified row costs git subprocesses plus a forge probe, so scanning rows
that accumulate forever (a Worktree row is only deleted by a successful
teardown, which a ticket closed off-pipeline never runs) timed the provision
path out against the smoke's 60s budget. The scoping lives HERE, at the
caller: ``t3 recover`` keeps the unscoped default scan (hold verdict 1296 on
#4814) so its data-loss audit never loses a terminal ticket's unpushed branch.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.gates.orphan_guard import BranchReport, BranchStatus
from teatree.core.management.commands._workspace.helpers import warn_orphans
from teatree.core.models import Ticket, Worktree


def _fake_workspace() -> MagicMock:
    """A MagicMock workspace whose ``/ x`` yields an existing path string."""
    fake = MagicMock()

    def _div(_self: object, x: str) -> MagicMock:
        return MagicMock(spec=Path, is_dir=lambda: True, __str__=lambda _s: f"/ws/{x}")

    fake.__truediv__ = _div
    return fake


class TestWarnOrphansScopesTheScanToInFlightRows(TestCase):
    @patch("teatree.core.gates.orphan_guard.classify_branch")
    @patch("teatree.core.gates.orphan_guard.clone_root")
    def test_a_terminal_ticket_row_is_never_classified_nor_warned(
        self,
        mock_clone_root: MagicMock,
        mock_classify: MagicMock,
    ) -> None:
        mock_clone_root.return_value = _fake_workspace()
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/alpha/-/issues/delivered",
            state=Ticket.State.DELIVERED,
        )
        Worktree.objects.create(overlay="test", ticket=ticket, repo_path="org/alpha", branch="feat-delivered")

        lines: list[str] = []
        warn_orphans(lines.append)

        mock_classify.assert_not_called()
        assert lines == []

    @patch("teatree.core.gates.orphan_guard.classify_branch")
    @patch("teatree.core.gates.orphan_guard.clone_root")
    def test_an_in_flight_row_is_still_classified_and_warned(
        self,
        mock_clone_root: MagicMock,
        mock_classify: MagicMock,
    ) -> None:
        mock_clone_root.return_value = _fake_workspace()
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/alpha/-/issues/live",
            state=Ticket.State.REVIEW_REQUESTED,
        )
        Worktree.objects.create(overlay="test", ticket=ticket, repo_path="org/alpha", branch="feat-live")
        mock_classify.return_value = BranchReport(
            repo="/ws/org/alpha",
            branch="feat-live",
            status=BranchStatus.UNPUSHED_ORPHAN,
            ahead_count=3,
        )

        lines: list[str] = []
        warn_orphans(lines.append)

        mock_classify.assert_called_once_with("/ws/org/alpha", "feat-live")
        assert lines
        assert "WARNING" in lines[0]
        assert any("feat-live" in line for line in lines)
