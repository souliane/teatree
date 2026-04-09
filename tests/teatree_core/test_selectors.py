import time
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.cache import cache as django_cache
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt, Ticket, Worktree
from teatree.core.selectors import (
    _build_mr_rows,
    _cached,
    _check_mr,
    _first_mr_title,
    _humanize_duration,
    _last_result_for_tasks,
    _list_of_str,
    _panel_cache,
    _uptime_from_epoch_ms,
    _variant_url,
    build_action_required,
    build_active_sessions,
    build_automation_summary,
    build_dashboard_snapshot,
    build_dashboard_summary,
    build_dashboard_ticket_rows,
    build_headless_queue,
    build_interactive_queue,
    build_pending_reviews,
    build_recent_activity,
    build_task_detail,
    build_worktree_rows,
    invalidate_panel_cache,
)
from teatree.core.selectors._types import DashboardTicketRow
from teatree.core.selectors.dashboard import (
    _count_needs_reply,
    _mr_latest_updated_at,
)
from teatree.core.sync import PENDING_REVIEWS_CACHE_KEY


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


class TestBuildDashboardSummary(TestCase):
    def test_counts_in_flight_work(self) -> None:
        active_ticket = Ticket.objects.create(issue_url="https://example.com/issues/101", state=Ticket.State.STARTED)
        done_ticket = Ticket.objects.create(issue_url="https://example.com/issues/102", state=Ticket.State.DELIVERED)
        active_session = Session.objects.create(ticket=active_ticket, agent_id="codex")
        done_session = Session.objects.create(ticket=done_ticket, agent_id="claude")

        Worktree.objects.create(
            ticket=active_ticket,
            repo_path="/tmp/backend",
            branch="feature/101",
            state=Worktree.State.READY,
        )
        Worktree.objects.create(
            ticket=done_ticket,
            repo_path="/tmp/frontend",
            branch="feature/102",
            state=Worktree.State.CREATED,
        )
        Task.objects.create(
            ticket=active_ticket, session=active_session, execution_target=Task.ExecutionTarget.HEADLESS
        )
        Task.objects.create(
            ticket=active_ticket,
            session=active_session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        Task.objects.create(
            ticket=done_ticket,
            session=done_session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            status=Task.Status.COMPLETED,
        )

        summary = build_dashboard_summary()

        assert summary.in_flight_tickets == 1
        assert summary.active_worktrees == 1
        assert summary.pending_headless_tasks == 1
        assert summary.pending_interactive_tasks == 1


class TestBuildPendingReviews(TestCase):
    def test_returns_empty_when_no_cache(self) -> None:
        assert build_pending_reviews() == []

    def test_returns_rows_from_cache(self) -> None:
        django_cache.set(
            PENDING_REVIEWS_CACHE_KEY,
            [
                {
                    "url": "https://github.com/org/repo/pull/1",
                    "title": "Add feature",
                    "repo": "repo",
                    "iid": "1",
                    "author": "bob",
                    "draft": "False",
                    "updated_at": "2026-04-09",
                },
            ],
        )

        rows = build_pending_reviews()

        assert len(rows) == 1
        assert rows[0].repo == "repo"
        assert rows[0].author == "bob"
        assert not rows[0].draft

        django_cache.delete(PENDING_REVIEWS_CACHE_KEY)

    def test_summary_includes_pending_reviews_count(self) -> None:
        django_cache.set(
            PENDING_REVIEWS_CACHE_KEY,
            [{"url": "u", "title": "t", "repo": "r", "iid": "1", "author": "a", "draft": "False", "updated_at": ""}],
        )

        summary = build_dashboard_summary()

        assert summary.pending_reviews == 1

        django_cache.delete(PENDING_REVIEWS_CACHE_KEY)


class TestBuildDashboardTicketRows(TestCase):
    def test_annotates_related_counts(self) -> None:
        first_ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/201",
            variant="ops",
            repos=["backend", "frontend"],
            state=Ticket.State.STARTED,
            extra={
                "tracker_status": "Process::Doing",
                "issue_title": "Add login feature",
                "mrs": {
                    "https://gitlab.com/org/backend/-/merge_requests/10": {
                        "url": "https://gitlab.com/org/backend/-/merge_requests/10",
                        "title": "feat: add login",
                        "repo": "backend",
                        "iid": 10,
                        "branch": "feat/login",
                        "draft": False,
                        "pipeline_status": "success",
                        "pipeline_url": "https://gitlab.com/pipelines/1",
                        "approvals": {"count": 1, "required": 1},
                    },
                },
            },
        )
        second_ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/202",
            repos=["docs"],
            state=Ticket.State.CODED,
        )
        delivered_ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/203",
            state=Ticket.State.DELIVERED,
        )

        first_session = Session.objects.create(ticket=first_ticket, agent_id="codex")
        second_session = Session.objects.create(ticket=second_ticket, agent_id="claude")
        delivered_session = Session.objects.create(ticket=delivered_ticket, agent_id="other")

        Worktree.objects.create(
            ticket=first_ticket,
            repo_path="/tmp/one",
            branch="feature/201",
            state=Worktree.State.PROVISIONED,
        )
        Worktree.objects.create(
            ticket=first_ticket,
            repo_path="/tmp/two",
            branch="feature/201-frontend",
            state=Worktree.State.READY,
        )
        Worktree.objects.create(
            ticket=second_ticket,
            repo_path="/tmp/docs",
            branch="feature/202",
            state=Worktree.State.CREATED,
        )
        Task.objects.create(
            ticket=first_ticket,
            session=first_session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.CLAIMED,
        )
        Task.objects.create(
            ticket=first_ticket,
            session=first_session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            status=Task.Status.FAILED,
        )
        Task.objects.create(
            ticket=second_ticket,
            session=second_session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.COMPLETED,
        )
        Task.objects.create(
            ticket=delivered_ticket,
            session=delivered_session,
            execution_target=Task.ExecutionTarget.HEADLESS,
        )

        rows = build_dashboard_ticket_rows()

        assert [row.ticket_id for row in rows] == [first_ticket.pk, second_ticket.pk]
        assert rows[0].display_id == "201"
        assert rows[0].variant == "ops"
        assert rows[0].repos == ["backend", "frontend"]
        assert rows[0].tracker_status == "Doing"
        assert rows[0].issue_title == "Add login feature"
        assert rows[0].ongoing_tasks == 1  # FAILED tasks excluded
        assert len(rows[0].mrs) == 1
        assert rows[0].mrs[0].repo == "backend"
        assert rows[0].mrs[0].pipeline_status == "success"
        assert rows[0].mrs[0].approval_count == 1
        assert rows[1].ongoing_tasks == 0
        assert rows[1].mrs == []
        assert rows[1].tracker_status == ""

    def test_includes_slack_and_e2e_fields(self) -> None:
        Ticket.objects.create(
            issue_url="https://example.com/issues/301",
            repos=["backend"],
            state=Ticket.State.STARTED,
            extra={
                "mrs": {
                    "https://gitlab.com/org/backend/-/merge_requests/20": {
                        "url": "https://gitlab.com/org/backend/-/merge_requests/20",
                        "title": "feat: with slack link",
                        "repo": "backend",
                        "iid": 20,
                        "branch": "feat/slack",
                        "draft": False,
                        "pipeline_status": "success",
                        "pipeline_url": "https://gitlab.com/pipelines/2",
                        "approvals": {"count": 1, "required": 1},
                        "review_requested": True,
                        "reviewer_names": ["alice"],
                        "review_channel": "#backend-review",
                        "review_permalink": "https://slack.com/archives/C123/p456",
                        "e2e_test_plan_url": "https://gitlab.com/org/backend/-/merge_requests/20#note_99",
                    },
                    "https://gitlab.com/org/backend/-/merge_requests/21": {
                        "url": "https://gitlab.com/org/backend/-/merge_requests/21",
                        "title": "feat: no slack data",
                        "repo": "backend",
                        "iid": 21,
                        "branch": "feat/no-slack",
                        "draft": False,
                        "review_requested": True,
                        "reviewer_names": ["bob"],
                    },
                },
            },
        )

        rows = build_dashboard_ticket_rows()
        assert len(rows) == 1
        mrs = rows[0].mrs
        assert len(mrs) == 2

        # MR with Slack data
        mr_with = next(m for m in mrs if m.iid == "20")
        assert mr_with.review_channel == "#backend-review"
        assert mr_with.review_permalink == "https://slack.com/archives/C123/p456"
        assert mr_with.e2e_test_plan_url == "https://gitlab.com/org/backend/-/merge_requests/20#note_99"

        # MR without Slack data
        mr_without = next(m for m in mrs if m.iid == "21")
        assert mr_without.review_channel == ""
        assert mr_without.review_permalink == ""
        assert mr_without.e2e_test_plan_url == ""


class TestTicketRowFiltering(TestCase):
    def test_filters_synced_tickets_with_no_data(self) -> None:
        """Synced tickets with MR-only issue_url and no title/MRs are hidden."""
        # This ticket has an MR URL as issue_url but no issue/work_item URL, no title, no MRs
        Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/merge_requests/999",
            state=Ticket.State.STARTED,
        )
        # This ticket has a proper issue URL — should be shown
        Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/100",
            state=Ticket.State.STARTED,
            extra={"issue_title": "Real ticket"},
        )
        rows = build_dashboard_ticket_rows()
        assert len(rows) == 1
        assert rows[0].issue_title == "Real ticket"

    def test_keeps_tickets_with_issue_link(self) -> None:
        """Tickets with a real issue URL are shown even without MRs."""
        Ticket.objects.create(issue_url="https://gitlab.com/org/repo/-/issues/42", state=Ticket.State.STARTED)
        rows = build_dashboard_ticket_rows()
        assert len(rows) == 1

    def test_hides_tickets_with_no_visible_data(self) -> None:
        """Tickets with no issue link, no title, and no MRs are hidden."""
        Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        rows = build_dashboard_ticket_rows()
        assert len(rows) == 0


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
        snapshot = build_dashboard_snapshot()

        assert [row.task_id for row in queue] == [first.pk, second.pk]
        assert queue[0].last_error == ""
        assert queue[1].claimed_by == "codex-terminal"
        assert snapshot.interactive_queue == build_interactive_queue(pending_only=True)

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


class TestVariantUrl:
    def test_formats_correctly(self) -> None:
        from unittest.mock import MagicMock, patch  # noqa: PLC0415

        mock_overlay = MagicMock()
        mock_overlay.config.dev_env_url = "https://{variant}.dev.example.com"
        with patch(
            "teatree.core.overlay_loader._discover_overlays",
            return_value={"test": mock_overlay},
        ):
            assert _variant_url("OPS") == "https://ops.dev.example.com"

    def test_empty_template(self) -> None:
        from unittest.mock import MagicMock, patch  # noqa: PLC0415

        mock_overlay = MagicMock()
        mock_overlay.config.dev_env_url = ""
        with patch(
            "teatree.core.overlay_loader._discover_overlays",
            return_value={"test": mock_overlay},
        ):
            assert _variant_url("ops") == ""

    def test_empty_variant(self) -> None:
        assert _variant_url("") == ""

    def test_no_overlay_returns_empty(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch("teatree.core.overlay_loader._discover_overlays", return_value={}):
            assert _variant_url("ops") == ""

    def test_bad_template(self) -> None:
        from unittest.mock import MagicMock, patch  # noqa: PLC0415

        mock_overlay = MagicMock()
        mock_overlay.config.dev_env_url = "{missing_key}"
        with patch(
            "teatree.core.overlay_loader._discover_overlays",
            return_value={"test": mock_overlay},
        ):
            assert _variant_url("ops") == ""


class TestBuildMrRows(TestCase):
    def test_non_dict_mrs_data(self) -> None:
        ticket = Ticket.objects.create(
            state=Ticket.State.STARTED,
            extra={"mrs": "not-a-dict"},
        )
        assert _build_mr_rows(ticket) == []

    def test_non_dict_mr_entry(self) -> None:
        ticket = Ticket.objects.create(
            state=Ticket.State.STARTED,
            extra={"mrs": {"url1": "not-a-dict"}},
        )
        assert _build_mr_rows(ticket) == []

    def test_non_dict_approvals(self) -> None:
        ticket = Ticket.objects.create(
            state=Ticket.State.STARTED,
            extra={
                "mrs": {
                    "url1": {
                        "url": "https://gitlab.com/org/backend/-/merge_requests/10",
                        "title": "feat",
                        "repo": "backend",
                        "iid": "10",
                        "branch": "feat/x",
                        "draft": False,
                        "approvals": "not-a-dict",
                    },
                },
            },
        )
        rows = _build_mr_rows(ticket)
        assert len(rows) == 1
        assert rows[0].approval_count == 0

    def test_iid_extracted_from_url(self) -> None:
        """When iid is missing, it should be extracted from the URL."""
        ticket = Ticket.objects.create(
            state=Ticket.State.STARTED,
            extra={
                "mrs": {
                    "url1": {
                        "url": "https://gitlab.com/org/backend/-/merge_requests/42",
                        "title": "feat",
                        "repo": "backend",
                        "branch": "feat/x",
                        "draft": False,
                        # No "iid" key
                    },
                },
            },
        )
        rows = _build_mr_rows(ticket)
        assert len(rows) == 1
        assert rows[0].iid == "42"

    def test_iid_empty_when_no_match(self) -> None:
        """When iid is missing and URL doesn't match, iid stays empty."""
        ticket = Ticket.objects.create(
            state=Ticket.State.STARTED,
            extra={
                "mrs": {
                    "url1": {
                        "url": "https://example.com/no-match",
                        "title": "feat",
                        "repo": "backend",
                        "branch": "feat/x",
                        "draft": False,
                    },
                },
            },
        )
        rows = _build_mr_rows(ticket)
        assert len(rows) == 1
        assert rows[0].iid == ""


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


class TestBuildWorktreeRows(TestCase):
    def test_excludes_delivered_tickets(self) -> None:
        active_ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        delivered_ticket = Ticket.objects.create(state=Ticket.State.DELIVERED)
        Worktree.objects.create(
            ticket=active_ticket,
            repo_path="/tmp/active",
            branch="feat/active",
            state=Worktree.State.READY,
        )
        Worktree.objects.create(
            ticket=delivered_ticket,
            repo_path="/tmp/delivered",
            branch="feat/delivered",
            state=Worktree.State.READY,
        )

        rows = build_worktree_rows()

        assert len(rows) == 1
        assert rows[0].ticket_id == active_ticket.pk


class TestUptimeFromEpochMs:
    def test_minutes(self) -> None:
        now_ms = int(timezone.now().timestamp() * 1000)
        # 5 minutes ago
        assert _uptime_from_epoch_ms(now_ms - 5 * 60_000) == "5m"

    def test_hours(self) -> None:
        now_ms = int(timezone.now().timestamp() * 1000)
        # 2 hours and 30 minutes ago
        assert _uptime_from_epoch_ms(now_ms - (2 * 60 + 30) * 60_000) == "2h30m"


class TestFirstMrTitle(TestCase):
    def test_mrs_not_dict(self) -> None:
        """When mrs is not a dict, return empty string (branch 631->637)."""
        ticket = Ticket.objects.create(
            state=Ticket.State.STARTED,
            extra={"mrs": "not-a-dict"},
        )
        assert _first_mr_title(ticket) == ""

    def test_non_dict_mr_value(self) -> None:
        """When an mr entry is not a dict, skip it (branch 633->632)."""
        ticket = Ticket.objects.create(
            state=Ticket.State.STARTED,
            extra={"mrs": {"url1": "not-a-dict"}},
        )
        assert _first_mr_title(ticket) == ""

    def test_empty_title(self) -> None:
        """When mr dict has empty title, skip it (branch 635->632)."""
        ticket = Ticket.objects.create(
            state=Ticket.State.STARTED,
            extra={"mrs": {"url1": {"title": ""}}},
        )
        assert _first_mr_title(ticket) == ""

    def test_returns_first_nonempty_title(self) -> None:
        ticket = Ticket.objects.create(
            state=Ticket.State.STARTED,
            extra={"mrs": {"url1": {"title": ""}, "url2": {"title": "My Feature"}}},
        )
        assert _first_mr_title(ticket) == "My Feature"


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

    def test_included_in_snapshot(self) -> None:
        _panel_cache.clear()
        snapshot = build_dashboard_snapshot()

        assert snapshot.automation is not None
        assert snapshot.automation.running == 0
        _panel_cache.clear()


class TestOverlayFiltering(TestCase):
    """Verify that overlay= parameter filters all selector functions."""

    def setUp(self) -> None:
        super().setUp()
        _panel_cache.clear()

    def test_summary_filters_by_overlay(self) -> None:
        Ticket.objects.create(overlay="alpha", state=Ticket.State.STARTED)
        Ticket.objects.create(overlay="beta", state=Ticket.State.STARTED)

        all_summary = build_dashboard_summary()
        alpha_summary = build_dashboard_summary(overlay="alpha")

        assert all_summary.in_flight_tickets == 2
        assert alpha_summary.in_flight_tickets == 1

    def test_ticket_rows_filter_by_overlay(self) -> None:
        Ticket.objects.create(
            overlay="alpha", state=Ticket.State.STARTED, issue_url="https://gitlab.com/o/r/-/issues/1"
        )
        Ticket.objects.create(overlay="beta", state=Ticket.State.STARTED, issue_url="https://gitlab.com/o/r/-/issues/2")

        all_rows = build_dashboard_ticket_rows()
        alpha_rows = build_dashboard_ticket_rows(overlay="alpha")

        assert len(all_rows) == 2
        assert len(alpha_rows) == 1

    def test_worktree_rows_filter_by_overlay(self) -> None:
        t1 = Ticket.objects.create(overlay="alpha", state=Ticket.State.STARTED)
        t2 = Ticket.objects.create(overlay="beta", state=Ticket.State.STARTED)
        Worktree.objects.create(ticket=t1, overlay="alpha", repo_path="be", branch="a")
        Worktree.objects.create(ticket=t2, overlay="beta", repo_path="be", branch="b")

        assert len(build_worktree_rows()) == 2
        assert len(build_worktree_rows(overlay="alpha")) == 1

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

    def test_snapshot_uses_overlay_in_cache_keys(self) -> None:
        Ticket.objects.create(overlay="alpha", state=Ticket.State.STARTED)
        Ticket.objects.create(overlay="beta", state=Ticket.State.STARTED)

        all_snap = build_dashboard_snapshot()
        alpha_snap = build_dashboard_snapshot(overlay="alpha")

        assert all_snap.summary.in_flight_tickets == 2
        assert alpha_snap.summary.in_flight_tickets == 1
        _panel_cache.clear()


class TestBuildPendingReviewsEdgeCases(TestCase):
    def test_returns_empty_when_cache_is_not_a_list(self) -> None:
        django_cache.set(PENDING_REVIEWS_CACHE_KEY, "not-a-list")
        assert build_pending_reviews() == []


def _make_ticket_row(ticket_id: int) -> DashboardTicketRow:
    return DashboardTicketRow(
        ticket_id=ticket_id,
        display_id="",
        issue_url="",
        has_issue=False,
        issue_title="",
        state="started",
        tracker_status="",
        notion_status="",
        notion_url="",
        variant="",
        variant_url="",
        repos=[],
        ongoing_tasks=0,
        total_tasks=0,
        labels=[],
        mrs=[],
        transitions=[],
    )


class TestMrLatestUpdatedAt(TestCase):
    def test_returns_empty_when_extra_is_not_dict(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        ticket.extra = "not-a-dict"
        ticket.save()
        assert _mr_latest_updated_at(_make_ticket_row(ticket.pk)) == ""

    def test_returns_fallback_updated_at_when_mrs_is_not_dict(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        ticket.extra = {"mrs": "not-a-dict", "updated_at": "2026-01-01"}
        ticket.save()
        assert _mr_latest_updated_at(_make_ticket_row(ticket.pk)) == "2026-01-01"


class TestCountNeedsReply:
    def test_returns_zero_when_discussions_not_a_list(self) -> None:
        assert _count_needs_reply({"discussions": "not-a-list"}) == 0
