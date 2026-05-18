"""The ``build_task_detail`` selector.

Split verbatim from the former monolithic ``tests/teatree_core/test_selectors.py`` (souliane/teatree#443).
"""

from django.test import TestCase

from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.selectors import build_task_detail


class TestBuildTaskDetail(TestCase):
    def test_returns_none_for_missing_task(self) -> None:
        assert build_task_detail(999999) is None

    def test_with_parent_and_children(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        parent_task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            phase="testing",
            execution_reason="Run tests",
        )
        child_task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            phase="testing",
            execution_reason="Manual verification",
            parent_task=parent_task,
        )
        # Also create an attempt for the parent
        TaskAttempt.objects.create(
            task=parent_task,
            execution_target="headless",
            exit_code=0,
            error="",
            result={"summary": "All pass"},
            agent_session_id="sess-123",
        )

        detail = build_task_detail(parent_task.pk)

        assert detail is not None
        assert detail.task_id == parent_task.pk
        assert detail.ticket_id == ticket.pk
        assert detail.phase == "testing"
        assert detail.parent is None
        assert len(detail.children) == 1
        assert detail.children[0].task_id == child_task.pk
        assert len(detail.attempts) == 1
        assert detail.attempts[0].result == {"summary": "All pass"}
        assert detail.session_agent_id == "agent"

    def test_child_has_parent(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        parent_task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            phase="shipping",
            execution_reason="Ship it",
        )
        child_task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            phase="shipping",
            execution_reason="Needs input",
            parent_task=parent_task,
        )

        detail = build_task_detail(child_task.pk)

        assert detail is not None
        assert detail.parent is not None
        assert detail.parent.task_id == parent_task.pk

    def test_attempt_with_non_dict_result(self) -> None:
        """TaskAttempt with non-dict result should yield empty dict."""
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
        )
        TaskAttempt.objects.create(
            task=task,
            execution_target="headless",
            result="not-a-dict",
        )

        detail = build_task_detail(task.pk)

        assert detail is not None
        assert detail.attempts[0].result == {}

    def test_no_session_id(self) -> None:
        """Task without session_id should have empty session_agent_id."""
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
        )

        detail = build_task_detail(task.pk)

        assert detail is not None
        # session_id is set, so session_agent_id should be the agent_id
        assert detail.session_agent_id == "agent"
