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

import pytest
from django.test import TestCase

from teatree.core.gates.orphan_guard import BranchReport, BranchStatus
from teatree.core.management.commands._workspace.helpers import warn_orphans
from teatree.core.models import Ticket, Worktree
from tests._git_repo import make_git_repo, run_git


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


class TestAnUnreadableRemoteIsAWarningNotARefusal(TestCase):
    """An in-flight branch whose remote cannot be read is still warned about, never a crash."""

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def test_the_orphan_is_warned_with_its_remote_state_unknown(self) -> None:
        make_git_repo(self._tmp_path / "origin.git", bare=True)
        clone = make_git_repo(self._tmp_path / "clone")
        run_git(clone, "remote", "add", "origin", str(self._tmp_path / "origin.git"))
        run_git(clone, "push", "-q", "origin", "main")
        run_git(clone, "remote", "set-head", "origin", "main")
        run_git(clone, "checkout", "-q", "-b", "feat-unreadable")
        (clone / "feature.py").write_text("value = 1\n")
        run_git(clone, "add", "feature.py")
        run_git(clone, "commit", "-q", "-m", "feat: add the feature")
        ssh = self._tmp_path / "ssh-denied"
        ssh.write_text("#!/bin/sh\necho 'Permission denied (publickey).' >&2\nexit 255\n")
        ssh.chmod(0o755)
        run_git(clone, "config", "core.sshCommand", str(ssh))
        run_git(clone, "remote", "set-url", "origin", "git@forge.invalid:team/repo.git")
        ticket = Ticket.objects.create(
            issue_url="https://forge.invalid/team/repo/-/issues/1", state=Ticket.State.WORK_STARTED
        )
        Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="team/repo",
            branch="feat-unreadable",
            extra={"worktree_path": str(clone)},
        )

        lines: list[str] = []
        warn_orphans(lines.append)

        assert any("feat-unreadable" in line and BranchStatus.REMOTE_UNKNOWN.value in line for line in lines), lines
