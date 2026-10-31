"""``tasks list --branch`` and ``Task.objects.for_branch`` show only that branch's claimed and pending tasks."""

from typing import TYPE_CHECKING, cast

from django.core.management import call_command
from django.test import TestCase

from teatree.core.managers_phase_cadence import in_flight_for_branch
from teatree.core.models import Session, Task, Ticket, Worktree

if TYPE_CHECKING:
    from teatree.core.management.commands.tasks_session_view import TaskRow


def _task_on_branch(branch: str, *, status: str = Task.Status.PENDING) -> Task:
    ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
    Worktree.objects.create(ticket=ticket, repo_path=f"/tmp/{branch}", branch=branch)
    session = Session.objects.create(ticket=ticket, agent_id="coding")
    return Task.objects.create(ticket=ticket, session=session, phase="coding", status=status)


class TestTasksForBranch(TestCase):
    def test_the_manager_returns_only_claimed_and_pending_tasks_of_the_branch(self) -> None:
        pending = _task_on_branch("feat-a")
        claimed = _task_on_branch("feat-a", status=Task.Status.CLAIMED)
        _task_on_branch("feat-a", status=Task.Status.COMPLETED)
        _task_on_branch("feat-b")
        assert set(in_flight_for_branch(Task.objects.all(), "feat-a")) == {pending, claimed}

    def test_an_empty_branch_matches_nothing(self) -> None:
        _task_on_branch("feat-a")
        assert not in_flight_for_branch(Task.objects.all(), "").exists()

    def test_tasks_list_branch_shows_only_that_branchs_active_tasks(self) -> None:
        pending = _task_on_branch("feat-a")
        _task_on_branch("feat-a", status=Task.Status.FAILED)
        _task_on_branch("feat-b")
        rows = cast("list[TaskRow]", call_command("tasks", "list", "--branch", "feat-a"))
        assert [row["task_id"] for row in rows] == [pending.pk]

    def test_tasks_list_without_branch_still_lists_every_task(self) -> None:
        _task_on_branch("feat-a")
        _task_on_branch("feat-b", status=Task.Status.FAILED)
        rows = cast("list[TaskRow]", call_command("tasks", "list"))
        assert len(rows) == 2
