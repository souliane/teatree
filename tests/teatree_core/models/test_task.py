"""Task model tests (souliane/teatree#443 split of test_models.py).

Lifecycle, child-task spawning, and ``build_task_detail``.
"""

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import (
    ConfigSetting,
    DeferredQuestion,
    InvalidTransitionError,
    Session,
    Task,
    TaskAttempt,
    Ticket,
)
from tests.teatree_core.models._shared import _advance_ticket_to_tested

_FAKE_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


class TestTask(TestCase):
    def test_claim_route_complete_fail_and_attempt_storage(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=ticket, session=session)

        task.claim(claimed_by="worker-1", lease_seconds=120)
        first_expiry = task.lease_expires_at
        assert first_expiry is not None

        task.renew_lease(lease_seconds=300)
        task.route_to_interactive(reason="needs manual follow-up")
        task.complete(result_artifact_path="/tmp/result.json")

        failed_task = Task.objects.create(ticket=ticket, session=session)
        failed_task.fail()

        attempt = TaskAttempt.objects.create(
            task=task,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            ended_at=timezone.now(),
            exit_code=0,
            artifact_path="/tmp/result.json",
        )

        task.refresh_from_db()
        failed_task.refresh_from_db()

        assert task.status == Task.Status.COMPLETED
        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE
        assert task.execution_reason == "needs manual follow-up"
        assert task.result_artifact_path == "/tmp/result.json"
        assert task.claimed_by == ""
        assert task.lease_expires_at is None
        assert failed_task.status == Task.Status.FAILED
        assert attempt.execution_target == Task.ExecutionTarget.INTERACTIVE
        assert str(task) == f"task-{task.pk}-{Task.ExecutionTarget.INTERACTIVE}"
        assert str(attempt) == f"attempt-{attempt.pk}"

    def test_claim_rejects_active_lease_and_sdk_routing_resets_claim(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        task.claim(claimed_by="worker-1")

        with pytest.raises(InvalidTransitionError, match="Task already claimed"):
            task.claim(claimed_by="worker-2")

        task.route_to_headless(reason="retry in sdk")
        task.refresh_from_db()

        assert task.execution_target == Task.ExecutionTarget.HEADLESS
        assert task.execution_reason == "retry in sdk"
        assert task.status == Task.Status.PENDING
        assert task.claimed_by == ""

    def test_claim_rejects_terminal_tasks(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        completed = Task.objects.create(ticket=ticket, session=session, status=Task.Status.COMPLETED)
        failed = Task.objects.create(ticket=ticket, session=session, status=Task.Status.FAILED)

        with pytest.raises(InvalidTransitionError, match="Task already finished"):
            completed.claim(claimed_by="worker-1")

        with pytest.raises(InvalidTransitionError, match="Task already finished"):
            failed.claim(claimed_by="worker-2")

    def test_complete_with_attempt_records_success_and_failure(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")

        success_task = Task.objects.create(ticket=ticket, session=session)
        attempt = success_task.complete_with_attempt(artifact_path="/tmp/ok.json")
        success_task.refresh_from_db()
        assert success_task.status == Task.Status.COMPLETED
        assert attempt.exit_code == 0
        assert attempt.artifact_path == "/tmp/ok.json"

        failure_task = Task.objects.create(ticket=ticket, session=session)
        attempt = failure_task.complete_with_attempt(exit_code=1, error="boom")
        failure_task.refresh_from_db()
        assert failure_task.status == Task.Status.FAILED
        assert attempt.exit_code == 1
        assert attempt.error == "boom"

    def test_parent_task_linkage_in_interactive_followup(self) -> None:
        ticket = Ticket.objects.create()
        _advance_ticket_to_tested(ticket)

        parent = ticket.tasks.get(phase="reviewing")
        parent.claim(claimed_by="worker")

        TaskAttempt.objects.create(
            task=parent,
            execution_target=parent.execution_target,
            exit_code=0,
            result={"needs_user_input": True, "user_input_reason": "Need input"},
        )
        parent.complete()

        child = ticket.tasks.filter(
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            status=Task.Status.PENDING,
        ).first()
        assert child is not None
        assert child.parent_task_id == parent.pk
        assert list(parent.child_tasks.values_list("pk", flat=True)) == [child.pk]

    def test_reopen_failed_task_resets_to_pending(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, status=Task.Status.FAILED)

        task.reopen()
        task.refresh_from_db()

        assert task.status == Task.Status.PENDING

    def test_reopen_non_failed_task_raises(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, status=Task.Status.PENDING)

        with pytest.raises(InvalidTransitionError, match="Can only reopen failed tasks"):
            task.reopen()


class TestNeedsUserInputHeadlessLane(TestCase):
    """A headless ``needs_user_input`` parks a correlated DeferredQuestion, not an interactive task.

    In the SDK/headless lane there is no human terminal to claim an
    interactive followup, so the durable record is a mirror-pending
    ``DeferredQuestion`` correlated to the parked task (its ``parked_task``
    FK). The tick-level poster scanner posts it to Slack; the reply re-queues
    a headless resume. The interactive lane keeps its in-session followup.
    """

    def _parked_headless_task(self, *, reason: str = "Which DB host?") -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id=_FAKE_UUID)
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            execution_target=Task.ExecutionTarget.HEADLESS,
        )
        TaskAttempt.objects.create(
            task=task,
            agent_session_id=_FAKE_UUID,
            result={"needs_user_input": True, "user_input_reason": reason},
        )
        return task

    def test_headless_runtime_records_correlated_deferred_question(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "sdk_oauth")
        task = self._parked_headless_task()

        task.complete()

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert not Task.objects.filter(execution_target=Task.ExecutionTarget.INTERACTIVE).exists()
        question = DeferredQuestion.objects.get()
        assert question.parked_task_id == task.pk
        assert "Which DB host?" in question.question
        assert question.slack_ts == ""
        assert question.is_pending

    def test_interactive_runtime_keeps_in_session_followup(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "interactive")
        task = self._parked_headless_task()

        task.complete()

        assert DeferredQuestion.objects.count() == 0
        followup = Task.objects.filter(
            parent_task=task,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        ).get()
        assert followup.parent_task_id == task.pk


class TestChildTaskSpawning(TestCase):
    def test_spawn_child_tasks_creates_per_repo_tasks(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="worker")
        parent = Task.objects.create(ticket=ticket, session=session, phase="coding")

        children = parent.spawn_child_tasks(["backend", "frontend", "translations"])

        assert len(children) == 3
        assert all(c.parent_task_id == parent.pk for c in children)
        assert all(c.phase == "coding" for c in children)
        assert [c.execution_reason for c in children] == [
            "Repo: backend",
            "Repo: frontend",
            "Repo: translations",
        ]

    def test_all_children_done(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        parent = Task.objects.create(ticket=ticket, session=session)
        children = parent.spawn_child_tasks(["a", "b"])

        assert not parent.all_children_done()

        children[0].status = Task.Status.COMPLETED
        children[0].save(update_fields=["status"])
        assert not parent.all_children_done()

        children[1].status = Task.Status.FAILED
        children[1].save(update_fields=["status"])
        assert parent.all_children_done()


class TestBuildTaskDetail(TestCase):
    def test_returns_full_lineage(self) -> None:
        from teatree.core.selectors import build_task_detail  # noqa: PLC0415

        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="test")
        parent = Task.objects.create(ticket=ticket, session=session, phase="coding")
        child = Task.objects.create(ticket=ticket, session=session, phase="reviewing", parent_task=parent)
        TaskAttempt.objects.create(
            task=parent,
            execution_target=Task.ExecutionTarget.HEADLESS,
            exit_code=0,
            result={"summary": "done", "files_modified": ["/a.py"]},
        )

        detail = build_task_detail(parent.pk)
        assert detail is not None
        assert detail.task_id == parent.pk
        assert detail.parent is None
        assert len(detail.children) == 1
        assert detail.children[0].task_id == child.pk
        assert len(detail.attempts) == 1
        assert detail.attempts[0].result == {"summary": "done", "files_modified": ["/a.py"]}

        child_detail = build_task_detail(child.pk)
        assert child_detail is not None
        assert child_detail.parent is not None
        assert child_detail.parent.task_id == parent.pk
        assert child_detail.children == []

    def test_returns_none_for_missing(self) -> None:
        from teatree.core.selectors import build_task_detail  # noqa: PLC0415

        assert build_task_detail(999999) is None
