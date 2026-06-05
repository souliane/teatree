"""Shared helpers for the teatree.core models test package.

Lifted verbatim from the former monolithic ``tests/teatree_core/test_models.py``
(souliane/teatree#443). No behavior change: the same ticket-advancement and
git-repo-setup helpers, relocated so each focused test module can import them.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import Task, Ticket, Worktree


def _start_with_provision(test_case: TestCase, ticket: Ticket) -> None:
    """Drive ``ticket.start()`` and let the worker schedule the planning task.

    Stage 3 of #140 made provisioning a worker side effect; tests that need
    the planning task materialised swap the worker for an inline stub so the
    side effect runs synchronously without touching real git.
    """
    from unittest.mock import MagicMock  # noqa: PLC0415

    from teatree.core import tasks as tasks_mod  # noqa: PLC0415

    def fake_enqueue(ticket_id: int) -> None:
        target = Ticket.objects.get(pk=ticket_id)
        if target.state == Ticket.State.STARTED:
            target.schedule_planning()

    fake_task = MagicMock()
    fake_task.enqueue.side_effect = fake_enqueue
    with (
        patch.object(tasks_mod, "execute_provision", fake_task),
        test_case.captureOnCommitCallbacks(execute=True),
    ):
        ticket.start()
        ticket.save()


def _advance_ticket_to_tested(ticket: Ticket, test_case: TestCase | None = None) -> None:
    """Advance a ticket through scoped, started, coded, tested.

    When ``test_case`` is provided, the start transition fires its on_commit
    callback so the coding task gets scheduled. Tests that don't care about
    the coding task can omit it.
    """
    ticket.scope(issue_url="https://example.com/issues/123", variant="acme", repos=["backend", "frontend"])
    ticket.save()
    if test_case is not None:
        _start_with_provision(test_case, ticket)
    else:
        ticket.start()
        ticket.save()
    _advance_started_to_planned(ticket)
    ticket.code()
    ticket.save()
    ticket.test(passed=True)
    ticket.save()


def _advance_started_to_planned(ticket: Ticket) -> None:
    """Record a PlanArtifact and drive STARTED → PLANNED so code() can run."""
    from teatree.core.models.plan_artifact import PlanArtifact  # noqa: PLC0415

    PlanArtifact.record(ticket=ticket, plan_text="Plan: implement the ticket", recorded_by="t3:planner")
    ticket.plan()
    ticket.save()


def _complete_phase_task(ticket: Ticket, phase: str) -> None:
    """Find the auto-scheduled task for a phase and complete it."""
    task = ticket.tasks.filter(phase=phase, status=Task.Status.PENDING).first()
    assert task is not None, f"No pending {phase} task found"
    task.claim(claimed_by="test-worker")
    task.complete()


def _attach_shippable_worktree(ticket: Ticket, tmp_path: Path, *, commits_ahead: int = 1) -> Worktree:
    """Attach a git-backed worktree to *ticket* so ``has_shippable_diff`` returns True."""
    repo_dir = tmp_path / f"repo-{ticket.pk}"
    branch = f"feature-{ticket.pk}"
    _init_repo_with_branch(repo_dir, branch=branch, commits_ahead=commits_ahead)
    return Worktree.objects.create(ticket=ticket, repo_path=str(repo_dir), branch=branch)


def _init_repo_with_branch(repo_dir: Path, *, branch: str, commits_ahead: int) -> None:
    """Initialise a git repo at *repo_dir* with main + a feature branch.

    The feature branch is named *branch* and contains *commits_ahead* commits
    on top of the main tip. Use ``commits_ahead=0`` for the no-shippable-diff
    case (branch points at the same SHA as main).
    """
    repo_dir.mkdir(parents=True, exist_ok=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo_dir), *args], check=True, env=env, capture_output=True)  # noqa: S607

    git("init", "--initial-branch=main")
    (repo_dir / "README.md").write_text("seed\n")
    git("add", "README.md")
    git("commit", "-m", "seed")
    git("checkout", "-b", branch)
    for i in range(commits_ahead):
        (repo_dir / f"f{i}.txt").write_text(f"{i}\n")
        git("add", f"f{i}.txt")
        git("commit", "-m", f"add f{i}")
