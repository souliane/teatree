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

    def _attach_unverifiable_worktree(self, ticket: Ticket) -> Path:
        """Attach a checkout PRESENT on disk whose commit-count probe cannot answer.

        A real directory that is not a git repository reproduces the venue-split
        condition without a mock: ``git rev-list`` exits 128 there exactly as it
        does for a checkout whose admin dir was written by another execution
        context and is unreachable from this one.
        """
        repo_dir = self._tmp_path / f"unverifiable-{ticket.pk}"
        repo_dir.mkdir()
        Worktree.objects.create(
            ticket=ticket,
            repo_path=str(repo_dir),
            branch=f"unverifiable-{ticket.pk}",
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

    def test_unverifiable_probe_fails_open(self) -> None:
        task = self._task()
        self._attach_unverifiable_worktree(task.ticket)
        assert landing_verification_error(task) == ""

    def test_unverifiable_sibling_suppresses_the_no_commit_refusal(self) -> None:
        # One worktree is verifiably commit-less, the other cannot be probed at
        # all — so "nothing landed" is not proved and the gate must not refuse.
        task = self._task()
        self._attach_worktree(task.ticket, commits_ahead=0)
        self._attach_unverifiable_worktree(task.ticket)
        assert landing_verification_error(task) == ""

    def test_unverifiable_sibling_does_not_mask_a_landed_commit(self) -> None:
        task = self._task()
        self._attach_unverifiable_worktree(task.ticket)
        self._attach_worktree(task.ticket, commits_ahead=1)
        assert landing_verification_error(task) == ""
