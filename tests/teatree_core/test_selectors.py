import time
from datetime import timedelta
from pathlib import Path

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.selectors import (
    _cached,
    _check_mr,
    _humanize_duration,
    _last_result_for_tasks,
    _list_of_str,
    _panel_cache,
    _uptime_from_epoch_ms,
    build_action_required,
    build_active_sessions,
    build_automation_summary,
    build_headless_queue,
    build_interactive_queue,
    build_recent_activity,
    build_task_detail,
    invalidate_panel_cache,
)


class TestCached:
    def test_returns_stored_value_within_ttl(self) -> None:
        _panel_cache.clear()
        calls: list[int] = []

        def builder() -> str:
            calls.append(1)
            return "fresh"

        assert _cached("test_key", builder, ttl=60.0) == "fresh"
        assert _cached("test_key", builder, ttl=60.0) == "fresh"
        assert len(calls) == 1
        _panel_cache.clear()

    def test_rebuilds_after_ttl_expires(self) -> None:
        _panel_cache.clear()
        calls: list[int] = []

        def builder() -> str:
            calls.append(1)
            return f"v{len(calls)}"

        # Populate cache with a stale entry (timestamp far in the past)
        _panel_cache["stale_key"] = (time.monotonic() - 100, "old")
        result = _cached("stale_key", builder, ttl=1.0)
        assert result == "v1"
        assert len(calls) == 1
        _panel_cache.clear()


class TestInvalidatePanelCache:
    def test_by_name(self) -> None:
        _panel_cache["a"] = (0.0, "val_a")
        _panel_cache["b"] = (0.0, "val_b")

        invalidate_panel_cache("a")

        assert "a" not in _panel_cache
        assert "b" in _panel_cache
        _panel_cache.clear()


class TestBuildInteractiveQueue(TestCase):
    def setUp(self) -> None:
        _panel_cache.clear()

    def test_returns_non_completed_manual_tasks(self) -> None:
        first_ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        second_ticket = Ticket.objects.create(state=Ticket.State.CODED)
        session = Session.objects.create(ticket=first_ticket, agent_id="codex")
        other_session = Session.objects.create(ticket=second_ticket, agent_id="claude")

        first = Task.objects.create(
            ticket=first_ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            execution_reason="Need reviewer decision",
        )
        second = Task.objects.create(
            ticket=second_ticket,
            session=other_session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            status=Task.Status.CLAIMED,
            claimed_by="codex-terminal",
        )
        Task.objects.create(
            ticket=second_ticket,
            session=other_session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            status=Task.Status.COMPLETED,
        )

        queue = build_interactive_queue()
        pending = build_interactive_queue(pending_only=True)

        assert [row.task_id for row in queue] == [first.pk, second.pk]
        assert queue[0].last_error == ""
        assert queue[1].claimed_by == "codex-terminal"
        assert pending == build_interactive_queue(pending_only=True)

    def test_includes_last_error_from_attempts(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="codex")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            status=Task.Status.CLAIMED,
        )
        TaskAttempt.objects.create(task=task, execution_target="interactive", exit_code=1, error="first error")
        TaskAttempt.objects.create(task=task, execution_target="interactive", exit_code=1, error="ttyd not found")

        queue = build_interactive_queue()

        assert len(queue) == 1
        assert queue[0].last_error == "ttyd not found"

    def test_excludes_failed_tasks(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        pending = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            status=Task.Status.FAILED,
        )

        queue = build_interactive_queue()

        assert [row.task_id for row in queue] == [pending.pk]


class TestBuildHeadlessQueue(TestCase):
    def test_excludes_failed_tasks(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        pending = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
        )
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.FAILED,
        )

        queue = build_headless_queue()

        assert [row.task_id for row in queue] == [pending.pk]

    def test_includes_result_summary(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.CLAIMED,
        )
        TaskAttempt.objects.create(
            task=task,
            execution_target="headless",
            exit_code=0,
            result={"summary": "Fixed 3 files"},
        )

        queue = build_headless_queue()

        assert len(queue) == 1
        assert queue[0].result_summary == "Fixed 3 files"

    def test_includes_session_and_phase(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="claude-headless")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            phase="testing",
        )

        queue = build_headless_queue()

        assert len(queue) == 1
        assert queue[0].task_id == task.pk
        assert queue[0].session_agent_id == "claude-headless"
        assert queue[0].phase == "testing"

    def test_includes_ticket_issue_url(self) -> None:
        ticket = Ticket.objects.create(
            state=Ticket.State.STARTED,
            issue_url="https://example.com/issues/555",
        )
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
        )

        queue = build_headless_queue()

        assert len(queue) == 1
        assert queue[0].issue_url == "https://example.com/issues/555"

    def test_include_dismissed(self) -> None:
        """include_dismissed=True should include FAILED tasks but not COMPLETED."""
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        failed = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.FAILED,
        )
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.COMPLETED,
        )
        pending = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
        )

        queue = build_headless_queue(include_dismissed=True)

        task_ids = [row.task_id for row in queue]
        assert failed.pk in task_ids
        assert pending.pk in task_ids


class TestHumanizeDuration:
    def test_seconds_only(self) -> None:
        assert _humanize_duration(45) == "45s"

    def test_minutes_and_seconds(self) -> None:
        assert _humanize_duration(150) == "2m 30s"

    def test_exact_minutes(self) -> None:
        assert _humanize_duration(120) == "2m"

    def test_hours_and_minutes(self) -> None:
        assert _humanize_duration(3900) == "1h 5m"

    def test_exact_hours(self) -> None:
        assert _humanize_duration(3600) == "1h"

    def test_zero(self) -> None:
        assert _humanize_duration(0) == "0s"

    def test_negative_clamps_to_zero(self) -> None:
        assert _humanize_duration(-5) == "0s"


class TestReapStaleClaims(TestCase):
    def test_reaps_claimed_tasks_with_expired_lease(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        now = timezone.now()
        stale = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.CLAIMED,
            claimed_by="dead-worker",
            lease_expires_at=now - timedelta(minutes=5),
        )
        active = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.CLAIMED,
            claimed_by="live-worker",
            lease_expires_at=now + timedelta(minutes=5),
        )

        reaped = Task.objects.reap_stale_claims()

        assert reaped == 1
        stale.refresh_from_db()
        active.refresh_from_db()
        assert stale.status == Task.Status.FAILED
        assert active.status == Task.Status.CLAIMED

    def test_queue_reaps_before_building(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        now = timezone.now()
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.CLAIMED,
            claimed_by="dead-worker",
            lease_expires_at=now - timedelta(minutes=5),
        )

        queue = build_headless_queue()

        assert len(queue) == 0


class TestHeadlessQueueElapsedTime(TestCase):
    def test_claimed_task_shows_elapsed_and_heartbeat(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        now = timezone.now()
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.CLAIMED,
            claimed_by="worker-1",
            claimed_at=now - timedelta(minutes=5),
            heartbeat_at=now - timedelta(seconds=30),
            lease_expires_at=now + timedelta(minutes=5),
        )

        queue = build_headless_queue()

        assert len(queue) == 1
        assert queue[0].elapsed_time  # non-empty
        assert "5m" in queue[0].elapsed_time
        assert queue[0].heartbeat_age  # non-empty
        assert "30s" in queue[0].heartbeat_age

    def test_pending_task_has_empty_elapsed(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
        )

        queue = build_headless_queue()

        assert len(queue) == 1
        assert queue[0].elapsed_time == ""
        assert queue[0].heartbeat_age == ""


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
        assert result[0].kind == "headless"
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


class TestCheckMr(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create(state=Ticket.State.STARTED)

    def test_returns_empty_for_draft(self) -> None:
        assert _check_mr({"draft": True}, self.ticket) == []

    def test_returns_empty_for_non_dict(self) -> None:
        assert _check_mr("not-a-dict", self.ticket) == []

    def test_returns_empty_for_merged(self) -> None:
        """Merged MRs must not surface as action items — bug hunt 2026-04-25 (#455 §2)."""
        assert _check_mr(self._closed_state_mr("merged"), self.ticket) == []

    def test_returns_empty_for_closed(self) -> None:
        """Closed-without-merge MRs must not surface as action items either."""
        assert _check_mr(self._closed_state_mr("closed"), self.ticket) == []

    @staticmethod
    def _closed_state_mr(state: str) -> dict:
        return {
            "draft": False,
            "repo": "backend",
            "iid": 10,
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "pipeline_status": "success",
            "state": state,
            "review_requested": True,
            "approvals": {"count": 0, "required": 2},
            "discussions": [{"status": "needs_reply"}],
        }

    def test_needs_review_request(self) -> None:
        mr = {
            "draft": False,
            "repo": "backend",
            "iid": 10,
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "pipeline_status": "success",
        }
        items = _check_mr(mr, self.ticket)
        assert len(items) == 1
        assert items[0].kind == "needs_review_request"

    def test_needs_reply(self) -> None:
        mr = {
            "draft": False,
            "repo": "backend",
            "iid": 10,
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "pipeline_status": "running",
            "discussions": [
                {"status": "needs_reply"},
                {"status": "needs_reply"},
            ],
        }
        items = _check_mr(mr, self.ticket)
        assert any(item.kind == "needs_reply" for item in items)
        needs_reply_item = next(i for i in items if i.kind == "needs_reply")
        assert "2 comments" in needs_reply_item.label

    def test_needs_reply_singular(self) -> None:
        mr = {
            "draft": False,
            "repo": "backend",
            "iid": 10,
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "discussions": [{"status": "needs_reply"}],
        }
        items = _check_mr(mr, self.ticket)
        assert any("1 comment need reply" in i.label for i in items)

    def test_needs_approval(self) -> None:
        mr = {
            "draft": False,
            "repo": "backend",
            "iid": 10,
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "pipeline_status": "success",
            "review_requested": True,
            "review_permalink": "https://slack.com/x",
            "approvals": {"count": 0, "required": 2},
        }
        items = _check_mr(mr, self.ticket)
        assert any(item.kind == "needs_approval" for item in items)

    def test_non_dict_approvals(self) -> None:
        """Non-dict approvals should be treated as empty."""
        mr = {
            "draft": False,
            "repo": "backend",
            "iid": 10,
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "pipeline_status": "success",
            "review_requested": True,
            "review_permalink": "https://slack.com/x",
            "approvals": "not-a-dict",
        }
        items = _check_mr(mr, self.ticket)
        assert any(item.kind == "needs_approval" for item in items)

    def test_non_list_discussions(self) -> None:
        """When discussions is not a list, the needs_reply check is skipped (branch 464->477)."""
        mr = {
            "draft": False,
            "repo": "backend",
            "iid": 10,
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "discussions": "not-a-list",
        }
        items = _check_mr(mr, self.ticket)
        # No crash; no needs_reply item
        assert all(i.kind != "needs_reply" for i in items)

    def test_review_draft_pending(self) -> None:
        mr = {
            "draft": False,
            "repo": "backend",
            "iid": 10,
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "draft_comments_pending": True,
            "draft_comments_count": 3,
        }
        items = _check_mr(mr, self.ticket)
        assert any(item.kind == "review_draft" for item in items)
        draft_item = next(i for i in items if i.kind == "review_draft")
        assert "3 draft comments" in draft_item.detail
        assert "agent posted review comments" in draft_item.label

    def test_review_draft_singular(self) -> None:
        mr = {
            "draft": False,
            "repo": "backend",
            "iid": 10,
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "draft_comments_pending": True,
            "draft_comments_count": 1,
        }
        items = _check_mr(mr, self.ticket)
        draft_item = next(i for i in items if i.kind == "review_draft")
        assert "1 draft comment need" in draft_item.detail

    def test_review_draft_not_pending(self) -> None:
        mr = {
            "draft": False,
            "repo": "backend",
            "iid": 10,
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "draft_comments_pending": False,
            "draft_comments_count": 0,
        }
        items = _check_mr(mr, self.ticket)
        assert all(i.kind != "review_draft" for i in items)

    def test_review_draft_missing_count(self) -> None:
        """When draft_comments_pending is True but count is missing, no item."""
        mr = {
            "draft": False,
            "repo": "backend",
            "iid": 10,
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "draft_comments_pending": True,
        }
        items = _check_mr(mr, self.ticket)
        assert all(i.kind != "review_draft" for i in items)


class TestBuildActionRequired(TestCase):
    def test_skips_non_dict_mrs(self) -> None:
        """When mrs is not a dict, it should be skipped."""
        Ticket.objects.create(
            state=Ticket.State.STARTED,
            extra={"mrs": "not-a-dict"},
        )

        items = build_action_required()

        assert all(item.kind == "interactive_task" for item in items) or items == []

    def test_includes_mr_action_items(self) -> None:
        """build_action_required iterates MRs and calls _check_mr (covers line 432)."""
        Ticket.objects.create(
            state=Ticket.State.STARTED,
            extra={
                "mrs": {
                    "url1": {
                        "draft": False,
                        "repo": "backend",
                        "iid": 10,
                        "url": "https://gitlab.com/org/backend/-/merge_requests/10",
                        "pipeline_status": "success",
                    },
                },
            },
        )

        items = build_action_required()

        assert any(i.kind == "needs_review_request" for i in items)


class TestListOfStr:
    def test_returns_empty_for_non_list(self) -> None:
        assert _list_of_str("not-a-list") == []

    def test_converts_elements(self) -> None:
        assert _list_of_str([1, "two", 3]) == ["1", "two", "3"]


class TestReviewCommentsInActionRequired(TestCase):
    """Review comments are now embedded in ActionRequiredItem via build_action_required."""

    def test_needs_reply_includes_review_comments(self) -> None:
        Ticket.objects.create(
            state=Ticket.State.STARTED,
            extra={
                "mrs": {
                    "url1": {
                        "url": "https://gitlab.com/org/backend/-/merge_requests/10",
                        "repo": "backend",
                        "iid": "10",
                        "discussions": [
                            {"status": "needs_reply", "detail": "Fix the bug"},
                            {"status": "addressed", "detail": "Done"},
                        ],
                    },
                },
            },
        )

        items = build_action_required()

        reply_items = [i for i in items if i.kind == "needs_reply"]
        assert len(reply_items) == 1
        assert len(reply_items[0].review_comments) == 2
        assert reply_items[0].review_comments[0].status == "Needs reply"
        assert reply_items[0].review_comments[1].status == "Addressed"

    def test_needs_reply_includes_slack_url(self) -> None:
        Ticket.objects.create(
            state=Ticket.State.STARTED,
            extra={
                "mrs": {
                    "url1": {
                        "url": "https://gitlab.com/org/backend/-/merge_requests/10",
                        "repo": "backend",
                        "iid": "10",
                        "review_permalink": "https://slack.com/archives/C123/p456",
                        "discussions": [
                            {"status": "needs_reply", "detail": "Fix it"},
                        ],
                    },
                },
            },
        )

        items = build_action_required()

        reply_items = [i for i in items if i.kind == "needs_reply"]
        assert len(reply_items) == 1
        assert reply_items[0].slack_url == "https://slack.com/archives/C123/p456"

    def test_skips_non_dict_discussions(self) -> None:
        Ticket.objects.create(
            state=Ticket.State.STARTED,
            extra={
                "mrs": {
                    "url1": {
                        "url": "https://gitlab.com/org/x/-/merge_requests/1",
                        "repo": "x",
                        "iid": "1",
                        "discussions": "not-a-list",
                    },
                },
            },
        )

        items = build_action_required()
        reply_items = [i for i in items if i.kind == "needs_reply"]
        assert reply_items == []

    def test_skips_non_dict_discussion_entries(self) -> None:
        Ticket.objects.create(
            state=Ticket.State.STARTED,
            extra={
                "mrs": {
                    "url1": {
                        "url": "https://gitlab.com/org/x/-/merge_requests/1",
                        "repo": "x",
                        "iid": "1",
                        "discussions": ["not-a-dict"],
                    },
                },
            },
        )

        items = build_action_required()
        reply_items = [i for i in items if i.kind == "needs_reply"]
        assert reply_items == []


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


class TestUptimeFromEpochMs:
    def test_minutes(self) -> None:
        now_ms = int(timezone.now().timestamp() * 1000)
        # 5 minutes ago
        assert _uptime_from_epoch_ms(now_ms - 5 * 60_000) == "5m"

    def test_hours(self) -> None:
        now_ms = int(timezone.now().timestamp() * 1000)
        # 2 hours and 30 minutes ago
        assert _uptime_from_epoch_ms(now_ms - (2 * 60 + 30) * 60_000) == "2h30m"


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


class TestBuildAutomationSummary(TestCase):
    def test_counts_headless_activity(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        running_task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.CLAIMED,
        )
        completed_task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.COMPLETED,
        )
        failed_task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.FAILED,
        )
        # Successful attempt
        TaskAttempt.objects.create(
            task=completed_task,
            execution_target="headless",
            exit_code=0,
            ended_at=timezone.now(),
        )
        # Failed attempt
        TaskAttempt.objects.create(
            task=failed_task,
            execution_target="headless",
            exit_code=1,
            ended_at=timezone.now(),
        )
        # Running attempt (no ended_at)
        TaskAttempt.objects.create(
            task=running_task,
            execution_target="headless",
        )

        summary = build_automation_summary()

        assert summary.running == 1
        assert summary.completed_24h == 2
        assert summary.succeeded_24h == 1
        assert summary.failed_24h == 1

    def test_excludes_old_attempts(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.COMPLETED,
        )
        old_time = timezone.now() - timezone.timedelta(hours=25)
        TaskAttempt.objects.create(
            task=task,
            execution_target="headless",
            exit_code=0,
            ended_at=old_time,
        )

        summary = build_automation_summary()

        assert summary.completed_24h == 0
        assert summary.succeeded_24h == 0

    def test_last_completed_at(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.COMPLETED,
        )
        now = timezone.now()
        TaskAttempt.objects.create(
            task=task,
            execution_target="headless",
            exit_code=0,
            ended_at=now,
        )

        summary = build_automation_summary()

        assert summary.last_completed_at == now.isoformat()

    def test_aggregates_token_usage(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        for input_t, output_t, cost in [(1000, 500, 0.01), (2000, 800, 0.02)]:
            task = Task.objects.create(
                ticket=ticket,
                session=session,
                execution_target=Task.ExecutionTarget.HEADLESS,
                status=Task.Status.COMPLETED,
            )
            TaskAttempt.objects.create(
                task=task,
                execution_target="headless",
                exit_code=0,
                ended_at=timezone.now(),
                input_tokens=input_t,
                output_tokens=output_t,
                cost_usd=cost,
            )

        summary = build_automation_summary()

        assert summary.total_tokens_24h == 4300
        assert summary.total_cost_24h == pytest.approx(0.03)

    def test_empty_state(self) -> None:
        summary = build_automation_summary()

        assert summary.running == 0
        assert summary.completed_24h == 0
        assert summary.succeeded_24h == 0
        assert summary.failed_24h == 0
        assert summary.last_completed_at == ""
        assert summary.total_tokens_24h == 0
        assert summary.total_cost_24h == pytest.approx(0.0)


class TestOverlayFiltering(TestCase):
    """Verify that overlay= parameter filters all selector functions."""

    def setUp(self) -> None:
        super().setUp()
        _panel_cache.clear()

    def test_headless_queue_filters_by_overlay(self) -> None:
        t1 = Ticket.objects.create(overlay="alpha")
        t2 = Ticket.objects.create(overlay="beta")
        s1 = Session.objects.create(ticket=t1, overlay="alpha")
        s2 = Session.objects.create(ticket=t2, overlay="beta")
        Task.objects.create(ticket=t1, session=s1, execution_target="headless")
        Task.objects.create(ticket=t2, session=s2, execution_target="headless")

        assert len(build_headless_queue()) == 2
        assert len(build_headless_queue(overlay="alpha")) == 1

    def test_automation_summary_filters_by_overlay(self) -> None:
        t1 = Ticket.objects.create(overlay="alpha")
        t2 = Ticket.objects.create(overlay="beta")
        s1 = Session.objects.create(ticket=t1, overlay="alpha")
        s2 = Session.objects.create(ticket=t2, overlay="beta")
        task1 = Task.objects.create(ticket=t1, session=s1, execution_target="headless", status=Task.Status.CLAIMED)
        Task.objects.create(ticket=t2, session=s2, execution_target="headless", status=Task.Status.CLAIMED)
        TaskAttempt.objects.create(task=task1, execution_target="headless", exit_code=0, ended_at=timezone.now())

        all_summary = build_automation_summary()
        alpha_summary = build_automation_summary(overlay="alpha")

        assert all_summary.running == 2
        assert alpha_summary.running == 1
        assert alpha_summary.completed_24h == 1
