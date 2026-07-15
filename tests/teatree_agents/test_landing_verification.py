"""Landing verification for coding/debugging results (root-cause of the coder-yield stall).

A coding/debugging task that claims ``files_modified`` but landed no commit — the
coder spawned a background agent and yielded, or edited without committing — must
NOT be recorded COMPLETED. The completion chokepoint re-reads the ticket
worktree's git state and refuses with a ``landing_unverified`` failure unless a
new commit actually exists (HEAD advanced past the base, worktree not
dirty-uncommitted). When no materialised worktree is checkable, the gate
fails open — "couldn't verify" is not "did not land".
"""

from pathlib import Path

import pytest
from django.test import TestCase

from teatree.agents.landing_verification import landing_verification_error
from teatree.core.models import Session, Task, Ticket, Worktree
from tests.teatree_core.models._shared import _init_repo_with_branch


class TestLandingVerification(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _task(self, *, phase: str = "coding") -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id=phase)
        return Task.objects.create(ticket=ticket, session=session, phase=phase)

    def _attach_worktree(self, ticket: Ticket, *, commits_ahead: int) -> Path:
        repo_dir = self._tmp_path / f"repo-{ticket.pk}"
        branch = f"feature-{ticket.pk}"
        _init_repo_with_branch(repo_dir, branch=branch, commits_ahead=commits_ahead)
        Worktree.objects.create(
            ticket=ticket,
            repo_path=str(repo_dir),
            branch=branch,
            extra={"worktree_path": str(repo_dir)},
        )
        return repo_dir

    def test_commit_landed_and_clean_is_verified(self) -> None:
        task = self._task()
        self._attach_worktree(task.ticket, commits_ahead=1)
        assert landing_verification_error(task) == ""

    def test_no_new_commit_is_refused(self) -> None:
        task = self._task()
        self._attach_worktree(task.ticket, commits_ahead=0)
        error = landing_verification_error(task)
        assert error.startswith("landing_unverified:")
        assert "commit" in error.lower()

    def test_uncommitted_tracked_change_is_refused(self) -> None:
        task = self._task()
        repo_dir = self._attach_worktree(task.ticket, commits_ahead=1)
        (repo_dir / "f0.txt").write_text("edited but not committed\n")
        error = landing_verification_error(task)
        assert error.startswith("landing_unverified:")
        assert "uncommitted" in error.lower()

    def test_debugging_phase_is_also_verified(self) -> None:
        task = self._task(phase="debugging")
        self._attach_worktree(task.ticket, commits_ahead=0)
        assert landing_verification_error(task).startswith("landing_unverified:")

    def test_non_coding_phase_is_skipped(self) -> None:
        task = self._task(phase="reviewing")
        self._attach_worktree(task.ticket, commits_ahead=0)
        assert landing_verification_error(task) == ""

    def test_no_worktree_fails_open(self) -> None:
        task = self._task()
        assert landing_verification_error(task) == ""
