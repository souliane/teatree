"""Active-session, recent-activity and last-result selectors.

Split verbatim from the former monolithic ``tests/teatree_core/test_selectors.py`` (souliane/teatree#443).
"""

from pathlib import Path

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.selectors import _last_result_for_tasks, build_active_sessions, build_recent_activity


class TestRecentActivityTokens(TestCase):
    def test_includes_token_counts_and_cost(self) -> None:
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
            exit_code=0,
            ended_at=timezone.now(),
            input_tokens=1500,
            output_tokens=800,
            cost_usd=0.025,
        )

        rows = build_recent_activity()

        assert len(rows) == 1
        assert rows[0].input_tokens == 1500
        assert rows[0].output_tokens == 800
        assert rows[0].cost_usd is not None
        assert abs(rows[0].cost_usd - 0.025) < 1e-9

    def test_null_tokens_default_to_none(self) -> None:
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
            exit_code=0,
            ended_at=timezone.now(),
        )

        rows = build_recent_activity()

        assert len(rows) == 1
        assert rows[0].input_tokens is None
        assert rows[0].output_tokens is None
        assert rows[0].cost_usd is None


class TestBuildActiveSessions(TestCase):
    @pytest.fixture(autouse=True)
    def _setup_fixtures(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.monkeypatch = monkeypatch
        self.tmp_path = tmp_path

    def test_reads_claude_session_files(self) -> None:
        import json  # noqa: PLC0415
        import os  # noqa: PLC0415

        sessions_dir = self.tmp_path / "sessions"
        sessions_dir.mkdir()
        self.monkeypatch.setattr("teatree.core.selectors.activity._CLAUDE_SESSIONS_DIR", sessions_dir)

        # Create a session file with current process PID (guaranteed alive)
        current_pid = os.getpid()
        session_data = {
            "pid": current_pid,
            "sessionId": "test-session-abc",
            "cwd": "/tmp/test-project",
            "startedAt": int(timezone.now().timestamp() * 1000) - 300_000,
            "name": "my-session",
        }
        (sessions_dir / f"{current_pid}.json").write_text(json.dumps(session_data), encoding="utf-8")
        # Dead session (PID that doesn't exist)
        dead_data = {**session_data, "pid": 999_999_999, "sessionId": "dead"}
        (sessions_dir / "999999999.json").write_text(json.dumps(dead_data), encoding="utf-8")

        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.CLAIMED,
            claimed_by="worker",
            phase="coding",
            execution_target=Task.ExecutionTarget.HEADLESS,
        )
        TaskAttempt.objects.create(
            task=task,
            execution_target=task.execution_target,
            agent_session_id="test-session-abc",
        )
        # Claimed task without agent_session_id — exercises the falsy branch at selectors.py:521
        ticket2 = Ticket.objects.create(state=Ticket.State.STARTED)
        session2 = Session.objects.create(ticket=ticket2, agent_id="agent2")
        Task.objects.create(
            ticket=ticket2,
            session=session2,
            status=Task.Status.CLAIMED,
            claimed_by="worker2",
            phase="coding",
            execution_target=Task.ExecutionTarget.HEADLESS,
        )

        result = build_active_sessions()

        # Only the alive session should appear
        assert len(result) == 1
        assert result[0].pid == current_pid
        assert result[0].session_id == "test-session-abc"
        assert result[0].cwd == "/tmp/test-project"
        assert result[0].name == "my-session"
        assert result[0].task_id == task.pk
        assert result[0].ticket_id == ticket.pk
        # ``coding`` is loop-dispatched, so the Task.save() invariant routes it
        # to INTERACTIVE — the selector renders that as its kind.
        assert result[0].kind == "interactive"
        assert result[0].phase == "coding"

    def test_handles_invalid_json(self) -> None:
        import json  # noqa: PLC0415

        sessions_dir = self.tmp_path / "sessions"
        sessions_dir.mkdir()
        self.monkeypatch.setattr("teatree.core.selectors.activity._CLAUDE_SESSIONS_DIR", sessions_dir)

        # Invalid JSON file
        (sessions_dir / "bad.json").write_text("not valid json", encoding="utf-8")
        # Valid JSON but no pid
        (sessions_dir / "nopid.json").write_text(json.dumps({"sessionId": "x"}), encoding="utf-8")
        # Valid JSON, pid is not an int
        (sessions_dir / "strpid.json").write_text(json.dumps({"pid": "not-int", "sessionId": "y"}), encoding="utf-8")

        result = build_active_sessions()

        assert result == []

    def test_no_sessions_dir(self) -> None:
        """When _CLAUDE_SESSIONS_DIR is not a directory, return empty list."""
        self.monkeypatch.setattr("teatree.core.selectors.activity._CLAUDE_SESSIONS_DIR", self.tmp_path / "nonexistent")

        result = build_active_sessions()

        assert result == []

    def test_skips_sessions_for_completed_tasks(self) -> None:
        import json  # noqa: PLC0415
        import os  # noqa: PLC0415

        sessions_dir = self.tmp_path / "sessions"
        sessions_dir.mkdir()
        self.monkeypatch.setattr("teatree.core.selectors.activity._CLAUDE_SESSIONS_DIR", sessions_dir)

        current_pid = os.getpid()
        session_data = {"pid": current_pid, "sessionId": "done-session", "startedAt": 0}
        (sessions_dir / f"{current_pid}.json").write_text(json.dumps(session_data), encoding="utf-8")

        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.COMPLETED,
            phase="coding",
        )
        TaskAttempt.objects.create(
            task=task,
            execution_target=task.execution_target,
            agent_session_id="done-session",
        )

        result = build_active_sessions()
        assert result == []


class TestBuildRecentActivity(TestCase):
    def test_returns_ended_attempts(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            phase="testing",
        )
        TaskAttempt.objects.create(
            task=task,
            execution_target="headless",
            exit_code=0,
            ended_at=timezone.now(),
            result={"summary": "All pass"},
        )
        # Attempt without ended_at should not appear
        TaskAttempt.objects.create(
            task=task,
            execution_target="headless",
            exit_code=None,
        )

        rows = build_recent_activity()

        assert len(rows) == 1
        assert rows[0].result_summary == "All pass"
        assert rows[0].phase == "testing"

    def test_non_dict_result(self) -> None:
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
            exit_code=1,
            ended_at=timezone.now(),
            result="not-a-dict",
            error="Something went wrong in a very long error message that should be truncated",
        )

        rows = build_recent_activity()

        assert len(rows) == 1
        assert rows[0].result_summary == ""
        assert rows[0].error != ""


class TestLastResultForTasks(TestCase):
    def test_empty_list(self) -> None:
        """Empty task id list should return empty dict (loop doesn't execute)."""
        assert _last_result_for_tasks([]) == {}

    def test_returns_summary(self) -> None:
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
            result={"summary": "Done"},
        )

        result = _last_result_for_tasks([task.pk])

        assert result[task.pk] == "Done"

    def test_skips_empty_summary(self) -> None:
        """When attempt result has no summary, the task should not appear in results."""
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
            result={"other_key": "value"},  # No "summary" key -> empty summary
        )

        result = _last_result_for_tasks([task.pk])

        assert task.pk not in result
