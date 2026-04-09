"""Tests for the unified sessions selector."""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import Session, Task, Ticket
from teatree.core.selectors.unified import build_unified_sessions


class TestBuildUnifiedSessions(TestCase):
    def test_returns_empty_when_no_data(self) -> None:
        with patch("teatree.core.selectors.unified.build_active_sessions", return_value=[]):
            rows = build_unified_sessions()

        assert rows == []

    def test_includes_queued_tasks(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="test")
        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            execution_reason="Test task",
        )

        with patch("teatree.core.selectors.unified.build_active_sessions", return_value=[]):
            rows = build_unified_sessions()

        assert len(rows) == 1
        assert rows[0].row_status == "queued"
        assert rows[0].execution_reason == "Test task"

    def test_includes_completed_activity(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="test")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            execution_target=Task.ExecutionTarget.HEADLESS,
        )
        task.claim(claimed_by="test-agent")
        task.complete_with_attempt(exit_code=0, result={"summary": "Done"})

        with patch("teatree.core.selectors.unified.build_active_sessions", return_value=[]):
            rows = build_unified_sessions()

        assert len(rows) == 1
        assert rows[0].row_status == "completed"
        assert rows[0].result_summary == "Done"

    def test_includes_failed_activity(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="test")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            execution_target=Task.ExecutionTarget.HEADLESS,
        )
        task.claim(claimed_by="test-agent")
        task.complete_with_attempt(exit_code=1, error="Something broke")

        with patch("teatree.core.selectors.unified.build_active_sessions", return_value=[]):
            rows = build_unified_sessions()

        assert len(rows) == 1
        assert rows[0].row_status == "failed"

    def test_deduplicates_by_task_id(self) -> None:
        """Tasks that appear in both queued and activity should only appear once."""
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="test")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            execution_target=Task.ExecutionTarget.HEADLESS,
        )
        # Task is pending (queued) but also has a completed attempt
        task.claim(claimed_by="test")
        task.complete_with_attempt(exit_code=0, result={"summary": "Done"})

        with patch("teatree.core.selectors.unified.build_active_sessions", return_value=[]):
            rows = build_unified_sessions()

        task_ids = [r.task_id for r in rows]
        assert len(task_ids) == len(set(task_ids))
