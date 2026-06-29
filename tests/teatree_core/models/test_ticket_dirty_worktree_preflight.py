"""Clean-worktree preflight on the Ticket FSM transition path (#884).

Owner-resolved design: a tracked-dirty worktree at a ``code``/``test``/
``review``/``ship`` transition REFUSES the transition (the FSM does not
advance) and raises with a clear, actionable message naming the dirty
worktree. The held phase task is not force-reopened; recovery is the
existing lease-reaper safety net (per ``_refuse_if_worktree_dirty`` and
BLUEPRINT §4.1) — the worker stops heartbeating after the exception, the
lease expires, and ``reclaim_orphaned_claims`` returns the CLAIMED task
to PENDING on the next tick. No auto-stash — worktrees share ``.git`` so
a stash is repo-global (the foreign-stash hazard, near-miss class #806).

Untracked-only files do NOT block (the #925 distinction — a tracked
modification is the trigger, mirroring ``cli.update._tracked_dirty_paths``).

The second/third tests assert the guard is not vacuously over-blocking:
a clean worktree, a no-worktree ticket, and an untracked-only worktree
all transition normally.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import DirtyWorktreeError, Task, Ticket, Worktree
from tests.teatree_core.models._shared import (
    _advance_started_to_planned,
    _advance_ticket_to_tested,
    _complete_phase_task,
    _init_repo_with_branch,
)


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
        _advance_started_to_planned(ticket)
        # Modify a TRACKED file — the dirty state a transition must refuse.
        (repo_dir / "f0.txt").write_text("uncommitted tracked change\n")
        assert ticket.state == Ticket.State.PLANNED

        with pytest.raises(DirtyWorktreeError) as exc:
            ticket.code()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.PLANNED  # FSM did NOT advance
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

    def test_refusal_through_real_loop_path_rolls_back_and_task_is_reclaimable(self) -> None:
        """The real loop path: ``Task.complete()`` → … → guarded transition.

        ``Task.complete()`` wraps the task ``save()`` and the FSM transition
        in a single ``transaction.atomic()`` (#883). When the guarded
        ``code()`` transition raises ``DirtyWorktreeError`` the WHOLE outer
        atomic rolls back: the ticket does NOT advance AND the task reverts
        to its pre-``complete()`` state — CLAIMED, not COMPLETED, not a
        spuriously "reopened" PENDING. Recovery is the lease-reaper:
        ``reclaim_orphaned_claims`` returns the held CLAIMED task to PENDING
        once its lease expires (the worker stopped heartbeating after the
        exception). This is the actual post-rollback contract — the earlier
        test asserted a PENDING reopen that the real path never produces.

        Anti-vacuity: if ``_refuse_if_worktree_dirty`` is removed, ``code()``
        succeeds, the outer atomic commits, the task ends COMPLETED and the
        ticket advances to CODED — every assertion below flips. The guard is
        load-bearing for this test.
        """
        ticket = Ticket.objects.create()
        _wt, repo_dir = self._attach_worktree(ticket)
        ticket.scope()
        ticket.save()
        ticket.start()
        ticket.save()
        _advance_started_to_planned(ticket)
        coding_task = Task.objects.create(
            ticket=ticket,
            session=ticket.sessions.create(agent_id="coding"),
            phase="coding",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="coding",
        )
        coding_task.claim(claimed_by="worker", lease_seconds=300)
        (repo_dir / "f0.txt").write_text("dirty\n")

        # Drive the REAL loop path, not a bare ticket.code(): Task.complete()
        # opens the outer atomic, _advance_ticket → _apply_phase_transition
        # fires the guarded code() transition, which refuses.
        with pytest.raises(DirtyWorktreeError):
            coding_task.complete()

        ticket.refresh_from_db()
        coding_task.refresh_from_db()
        # FSM did NOT advance — the outer atomic rolled the code() advance back.
        assert ticket.state == Ticket.State.PLANNED
        # The task reverted to its pre-complete() state: CLAIMED (the outer
        # atomic rolled back the status=COMPLETED + _clear_claim writes too).
        assert coding_task.status == Task.Status.CLAIMED

        # Recovery contract: the held CLAIMED task is reclaimable by the
        # lease-reaper once its lease expires (worker stopped heartbeating).
        Task.objects.filter(pk=coding_task.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        reclaimed = Task.objects.reclaim_orphaned_claims()
        coding_task.refresh_from_db()
        assert reclaimed == 1
        assert coding_task.status == Task.Status.PENDING  # back on the queue for the agent to finish

    def test_clean_worktree_still_transitions_normally(self) -> None:
        """Guard must not over-block: a clean tracked tree advances as before."""
        ticket = Ticket.objects.create()
        self._attach_worktree(ticket)  # committed, clean
        ticket.scope()
        ticket.save()
        ticket.start()
        ticket.save()
        _advance_started_to_planned(ticket)

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
        _advance_started_to_planned(ticket)

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
        _advance_started_to_planned(ticket)
        # A brand-new untracked file only — no tracked modification.
        (repo_dir / "scratch_note.txt").write_text("untracked scratch\n")

        ticket.code()
        ticket.save()
        ticket.refresh_from_db()

        assert ticket.state == Ticket.State.CODED


class TestWorktreeTrackedDirtyPathFailOpen(TestCase):
    """Fail-open behaviour of ``worktree_tracked_dirty_path``.

    Returns ``None`` on an unverifiable worktree — the guard must not
    block on a state it cannot confirm, otherwise a legitimately-clean
    ticket stalls the loop.
    """

    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def test_returns_none_when_worktree_has_no_path(self) -> None:
        from teatree.core.models.ticket_worktree_checks import worktree_tracked_dirty_path  # noqa: PLC0415

        ticket = Ticket.objects.create()
        wt = Worktree.objects.create(ticket=ticket, repo_path="", branch="feature", extra={})

        assert worktree_tracked_dirty_path(wt) is None

    def test_returns_none_when_git_status_raises(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.core.models import ticket_worktree_checks as checks_mod  # noqa: PLC0415
        from teatree.core.models.ticket_worktree_checks import worktree_tracked_dirty_path  # noqa: PLC0415
        from teatree.utils.run import CommandFailedError  # noqa: PLC0415

        ticket = Ticket.objects.create()
        wt = Worktree.objects.create(
            ticket=ticket,
            repo_path=str(self._tmp_path),
            branch="feature",
            extra={"worktree_path": str(self._tmp_path)},
        )
        err = CommandFailedError(["git", "status", "--porcelain"], 128, "", "not a git repository")
        with patch.object(checks_mod.git, "status_porcelain", side_effect=err):
            assert worktree_tracked_dirty_path(wt) is None
