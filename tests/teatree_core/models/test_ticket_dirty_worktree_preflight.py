"""Clean-worktree preflight on the Ticket FSM transition path (#884).

Owner-resolved design: a tracked-dirty worktree at a ``code``/``test``/
``review``/``ship`` transition REFUSES the transition (the FSM does not
advance) and reopens the pending phase task with a clear, actionable
message naming the dirty worktree. No auto-stash — worktrees share
``.git`` so a stash is repo-global (the foreign-stash hazard, near-miss
class #806).

Untracked-only files do NOT block (the #925 distinction — a tracked
modification is the trigger, mirroring ``cli.update._tracked_dirty_paths``).

The second/third tests assert the guard is not vacuously over-blocking:
a clean worktree, a no-worktree ticket, and an untracked-only worktree
all transition normally.
"""

from pathlib import Path

import pytest
from django.test import TestCase

from teatree.core.models import DirtyWorktreeError, Task, Ticket, Worktree
from tests.teatree_core.models._shared import _advance_ticket_to_tested, _complete_phase_task, _init_repo_with_branch


class TestDirtyWorktreePreflightRefusesTransition(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _attach_worktree(self, ticket: Ticket, *, commits_ahead: int = 1) -> tuple[Worktree, Path]:
        repo_dir = self._tmp_path / f"repo-{ticket.pk}"
        branch = f"feature-{ticket.pk}"
        _init_repo_with_branch(repo_dir, branch=branch, commits_ahead=commits_ahead)
        wt = Worktree.objects.create(
            ticket=ticket,
            repo_path=str(repo_dir),
            branch=branch,
            extra={"worktree_path": str(repo_dir)},
        )
        return wt, repo_dir

    def test_code_transition_refused_when_worktree_tracked_dirty(self) -> None:
        ticket = Ticket.objects.create()
        _wt, repo_dir = self._attach_worktree(ticket)
        ticket.scope()
        ticket.save()
        ticket.start()
        ticket.save()
        # Modify a TRACKED file — the dirty state a transition must refuse.
        (repo_dir / "f0.txt").write_text("uncommitted tracked change\n")
        assert ticket.state == Ticket.State.STARTED

        with pytest.raises(DirtyWorktreeError) as exc:
            ticket.code()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED  # FSM did NOT advance
        assert str(repo_dir) in str(exc.value)  # message names the dirty worktree

    def test_ship_transition_refused_when_worktree_tracked_dirty(self) -> None:
        ticket = Ticket.objects.create()
        _wt, repo_dir = self._attach_worktree(ticket)
        _advance_ticket_to_tested(ticket)
        # test() auto-scheduled a reviewing task; completing it fires
        # review() (its condition needs a completed reviewing task) and
        # auto-schedules the shipping task.
        _complete_phase_task(ticket, "reviewing")
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED
        # Dirty the worktree after review, before ship.
        (repo_dir / "f0.txt").write_text("dirty before ship\n")

        with pytest.raises(DirtyWorktreeError) as exc:
            ticket.ship()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED  # ship did NOT advance
        assert str(repo_dir) in str(exc.value)

    def test_refused_transition_reopens_pending_phase_task(self) -> None:
        ticket = Ticket.objects.create()
        _wt, repo_dir = self._attach_worktree(ticket)
        ticket.scope()
        ticket.save()
        ticket.start()
        ticket.save()
        coding_task = Task.objects.create(
            ticket=ticket,
            session=ticket.sessions.create(agent_id="coding"),
            phase="coding",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="coding",
        )
        coding_task.claim(claimed_by="worker")
        (repo_dir / "f0.txt").write_text("dirty\n")

        with pytest.raises(DirtyWorktreeError):
            ticket.code()

        coding_task.refresh_from_db()
        assert coding_task.status == Task.Status.PENDING  # reopened for the agent to finish

    def test_clean_worktree_still_transitions_normally(self) -> None:
        """Guard must not over-block: a clean tracked tree advances as before."""
        ticket = Ticket.objects.create()
        self._attach_worktree(ticket)  # committed, clean
        ticket.scope()
        ticket.save()
        ticket.start()
        ticket.save()

        ticket.code()
        ticket.save()
        ticket.refresh_from_db()

        assert ticket.state == Ticket.State.CODED

    def test_no_worktree_ticket_still_transitions_normally(self) -> None:
        """No worktree means nothing can be dirty — must not block."""
        ticket = Ticket.objects.create()
        ticket.scope()
        ticket.save()
        ticket.start()
        ticket.save()

        ticket.code()
        ticket.save()
        ticket.refresh_from_db()

        assert ticket.state == Ticket.State.CODED

    def test_untracked_only_worktree_does_not_block(self) -> None:
        """#925 distinction: untracked-only files are not a refusal trigger."""
        ticket = Ticket.objects.create()
        _wt, repo_dir = self._attach_worktree(ticket)
        ticket.scope()
        ticket.save()
        ticket.start()
        ticket.save()
        # A brand-new untracked file only — no tracked modification.
        (repo_dir / "scratch_note.txt").write_text("untracked scratch\n")

        ticket.code()
        ticket.save()
        ticket.refresh_from_db()

        assert ticket.state == Ticket.State.CODED


class TestWorktreeTrackedDirtyPathFailOpen(TestCase):
    """Fail-open behaviour of ``_worktree_tracked_dirty_path``.

    Returns ``None`` on an unverifiable worktree — the guard must not
    block on a state it cannot confirm, otherwise a legitimately-clean
    ticket stalls the loop.
    """

    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def test_returns_none_when_worktree_has_no_path(self) -> None:
        from teatree.core.models.ticket import _worktree_tracked_dirty_path  # noqa: PLC0415

        ticket = Ticket.objects.create()
        wt = Worktree.objects.create(ticket=ticket, repo_path="", branch="feature", extra={})

        assert _worktree_tracked_dirty_path(wt) is None

    def test_returns_none_when_git_status_raises(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.core.models import ticket as ticket_mod  # noqa: PLC0415
        from teatree.core.models.ticket import _worktree_tracked_dirty_path  # noqa: PLC0415
        from teatree.utils.run import CommandFailedError  # noqa: PLC0415

        ticket = Ticket.objects.create()
        wt = Worktree.objects.create(
            ticket=ticket,
            repo_path=str(self._tmp_path),
            branch="feature",
            extra={"worktree_path": str(self._tmp_path)},
        )
        err = CommandFailedError(["git", "status", "--porcelain"], 128, "", "not a git repository")
        with patch.object(ticket_mod.git, "status_porcelain", side_effect=err):
            assert _worktree_tracked_dirty_path(wt) is None
