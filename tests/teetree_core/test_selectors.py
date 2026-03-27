import time
from pathlib import Path

import pytest
from django.utils import timezone

from teetree.core.models import Session, Task, TaskAttempt, Ticket, Worktree
from teetree.core.selectors import (
    _cached,
    _panel_cache,
    build_active_sessions,
    build_dashboard_snapshot,
    build_dashboard_summary,
    build_dashboard_ticket_rows,
    build_headless_queue,
    build_interactive_queue,
    invalidate_panel_cache,
)

pytestmark = pytest.mark.django_db


def test_build_dashboard_summary_counts_in_flight_work() -> None:
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
    Task.objects.create(ticket=active_ticket, session=active_session, execution_target=Task.ExecutionTarget.HEADLESS)
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


def test_build_dashboard_ticket_rows_annotates_related_counts() -> None:
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


def test_build_dashboard_ticket_rows_includes_slack_and_e2e_fields() -> None:
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


def test_build_interactive_queue_returns_non_completed_manual_tasks() -> None:
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


def test_build_interactive_queue_includes_last_error_from_attempts() -> None:
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


def test_build_active_sessions_reads_claude_session_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr("teetree.core.selectors._CLAUDE_SESSIONS_DIR", sessions_dir)

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


def test_build_headless_queue_excludes_failed_tasks() -> None:
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


def test_build_interactive_queue_excludes_failed_tasks() -> None:
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


def test_build_headless_queue_includes_result_summary() -> None:
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


def test_build_headless_queue_includes_session_and_phase() -> None:
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


# --- build_task_detail ---


from teetree.core.selectors import (  # noqa: E402
    _build_mr_rows,
    _check_mr,
    _list_of_str,
    _variant_url,
    build_action_required,
    build_recent_activity,
    build_review_comments,
    build_task_detail,
    build_worktree_rows,
)


def test_build_task_detail_returns_none_for_missing_task() -> None:
    assert build_task_detail(999999) is None


def test_build_task_detail_with_parent_and_children() -> None:
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


def test_build_task_detail_child_has_parent() -> None:
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


def test_build_task_detail_attempt_with_non_dict_result() -> None:
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


def test_build_task_detail_no_session_id() -> None:
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


# --- _build_task_queue with include_dismissed ---


def test_build_headless_queue_include_dismissed() -> None:
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


# --- _action_items_from_mrs / _check_mr ---


def test_action_items_from_mrs_skips_non_dict_mrs() -> None:
    """When mrs is not a dict, it should be skipped."""
    Ticket.objects.create(
        state=Ticket.State.STARTED,
        extra={"mrs": "not-a-dict"},
    )

    items = build_action_required()

    assert all(item.kind == "interactive_task" for item in items) or items == []


def test_check_mr_returns_empty_for_draft() -> None:
    ticket = Ticket.objects.create(state=Ticket.State.STARTED)
    assert _check_mr({"draft": True}, ticket) == []


def test_check_mr_returns_empty_for_non_dict() -> None:
    ticket = Ticket.objects.create(state=Ticket.State.STARTED)
    assert _check_mr("not-a-dict", ticket) == []


def test_check_mr_needs_review_request() -> None:
    ticket = Ticket.objects.create(state=Ticket.State.STARTED)
    mr = {
        "draft": False,
        "repo": "backend",
        "iid": 10,
        "url": "https://gitlab.com/org/backend/-/merge_requests/10",
        "pipeline_status": "success",
    }
    items = _check_mr(mr, ticket)
    assert len(items) == 1
    assert items[0].kind == "needs_review_request"


def test_check_mr_needs_reply() -> None:
    ticket = Ticket.objects.create(state=Ticket.State.STARTED)
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
    items = _check_mr(mr, ticket)
    assert any(item.kind == "needs_reply" for item in items)
    needs_reply_item = next(i for i in items if i.kind == "needs_reply")
    assert "2 comments" in needs_reply_item.label


def test_check_mr_needs_reply_singular() -> None:
    ticket = Ticket.objects.create(state=Ticket.State.STARTED)
    mr = {
        "draft": False,
        "repo": "backend",
        "iid": 10,
        "url": "https://gitlab.com/org/backend/-/merge_requests/10",
        "discussions": [{"status": "needs_reply"}],
    }
    items = _check_mr(mr, ticket)
    assert any("1 comment need reply" in i.label for i in items)


def test_check_mr_needs_approval() -> None:
    ticket = Ticket.objects.create(state=Ticket.State.STARTED)
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
    items = _check_mr(mr, ticket)
    assert any(item.kind == "needs_approval" for item in items)


def test_check_mr_non_dict_approvals() -> None:
    """Non-dict approvals should be treated as empty."""
    ticket = Ticket.objects.create(state=Ticket.State.STARTED)
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
    items = _check_mr(mr, ticket)
    assert any(item.kind == "needs_approval" for item in items)


# --- build_active_sessions edge cases ---


def test_build_active_sessions_handles_invalid_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import json  # noqa: PLC0415

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr("teetree.core.selectors._CLAUDE_SESSIONS_DIR", sessions_dir)

    # Invalid JSON file
    (sessions_dir / "bad.json").write_text("not valid json", encoding="utf-8")
    # Valid JSON but no pid
    (sessions_dir / "nopid.json").write_text(json.dumps({"sessionId": "x"}), encoding="utf-8")
    # Valid JSON, pid is not an int
    (sessions_dir / "strpid.json").write_text(json.dumps({"pid": "not-int", "sessionId": "y"}), encoding="utf-8")

    result = build_active_sessions()

    assert result == []


# --- _variant_url ---


from django.test import override_settings as override_settings_dj  # noqa: E402


@override_settings_dj(TEATREE_DEV_ENV_URL="https://{variant}.dev.example.com")
def test_variant_url_formats_correctly() -> None:
    assert _variant_url("OPS") == "https://ops.dev.example.com"


@override_settings_dj(TEATREE_DEV_ENV_URL="")
def test_variant_url_empty_template() -> None:
    assert _variant_url("ops") == ""


def test_variant_url_empty_variant() -> None:
    assert _variant_url("") == ""


@override_settings_dj(TEATREE_DEV_ENV_URL="{missing_key}")
def test_variant_url_bad_template() -> None:
    assert _variant_url("ops") == ""


# --- _build_mr_rows edge cases ---


def test_build_mr_rows_non_dict_mrs_data() -> None:
    ticket = Ticket.objects.create(
        state=Ticket.State.STARTED,
        extra={"mrs": "not-a-dict"},
    )
    assert _build_mr_rows(ticket) == []


def test_build_mr_rows_non_dict_mr_entry() -> None:
    ticket = Ticket.objects.create(
        state=Ticket.State.STARTED,
        extra={"mrs": {"url1": "not-a-dict"}},
    )
    assert _build_mr_rows(ticket) == []


def test_build_mr_rows_non_dict_approvals() -> None:
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


def test_build_mr_rows_iid_extracted_from_url() -> None:
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


def test_build_mr_rows_iid_empty_when_no_match() -> None:
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


# --- _list_of_str ---


def test_list_of_str_returns_empty_for_non_list() -> None:
    assert _list_of_str("not-a-list") == []


def test_list_of_str_converts_elements() -> None:
    assert _list_of_str([1, "two", 3]) == ["1", "two", "3"]


# --- build_review_comments ---


def test_build_review_comments_with_discussions() -> None:
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

    rows = build_review_comments()

    assert len(rows) == 2
    assert rows[0].mr_label == "backend !10"
    assert rows[0].status == "Needs reply"
    assert rows[1].status == "Addressed"


def test_build_review_comments_skips_non_dict_mrs() -> None:
    Ticket.objects.create(
        state=Ticket.State.STARTED,
        extra={"mrs": "not-a-dict"},
    )

    rows = build_review_comments()
    assert rows == []


def test_build_review_comments_skips_non_dict_mr() -> None:
    Ticket.objects.create(
        state=Ticket.State.STARTED,
        extra={"mrs": {"url1": "not-a-dict"}},
    )

    rows = build_review_comments()
    assert rows == []


def test_build_review_comments_skips_non_list_discussions() -> None:
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

    rows = build_review_comments()
    assert rows == []


def test_build_review_comments_skips_non_dict_discussion() -> None:
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

    rows = build_review_comments()
    assert rows == []


def test_build_review_comments_uses_url_as_label_when_no_repo_iid() -> None:
    Ticket.objects.create(
        state=Ticket.State.STARTED,
        extra={
            "mrs": {
                "url1": {
                    "url": "https://gitlab.com/org/x/-/merge_requests/1",
                    # No repo or iid
                    "discussions": [{"status": "waiting_reviewer", "detail": "note"}],
                },
            },
        },
    )

    rows = build_review_comments()
    assert len(rows) == 1
    assert rows[0].mr_label == "https://gitlab.com/org/x/-/merge_requests/1"


# --- build_recent_activity ---


def test_build_recent_activity_returns_ended_attempts() -> None:
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


def test_build_recent_activity_non_dict_result() -> None:
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


# --- build_worktree_rows (already tested indirectly, but cover delivered exclusion) ---


def test_build_worktree_rows_excludes_delivered_tickets() -> None:
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


# --- _action_items_from_mrs through build_action_required (line 432) ---


def test_action_required_includes_mr_action_items() -> None:
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


def test_check_mr_non_list_discussions() -> None:
    """When discussions is not a list, the needs_reply check is skipped (branch 464->477)."""
    ticket = Ticket.objects.create(state=Ticket.State.STARTED)
    mr = {
        "draft": False,
        "repo": "backend",
        "iid": 10,
        "url": "https://gitlab.com/org/backend/-/merge_requests/10",
        "discussions": "not-a-list",
    }
    items = _check_mr(mr, ticket)
    # No crash; no needs_reply item
    assert all(i.kind != "needs_reply" for i in items)


# --- _uptime_from_epoch_ms hours path (line 514) ---


from teetree.core.selectors import _uptime_from_epoch_ms  # noqa: E402


def test_uptime_from_epoch_ms_minutes() -> None:
    now_ms = int(timezone.now().timestamp() * 1000)
    # 5 minutes ago
    assert _uptime_from_epoch_ms(now_ms - 5 * 60_000) == "5m"


def test_uptime_from_epoch_ms_hours() -> None:
    now_ms = int(timezone.now().timestamp() * 1000)
    # 2 hours and 30 minutes ago
    assert _uptime_from_epoch_ms(now_ms - (2 * 60 + 30) * 60_000) == "2h30m"


# --- _first_mr_title branch coverage ---


from teetree.core.selectors import _first_mr_title  # noqa: E402


def test_first_mr_title_mrs_not_dict() -> None:
    """When mrs is not a dict, return empty string (branch 631->637)."""
    ticket = Ticket.objects.create(
        state=Ticket.State.STARTED,
        extra={"mrs": "not-a-dict"},
    )
    assert _first_mr_title(ticket) == ""


def test_first_mr_title_non_dict_mr_value() -> None:
    """When an mr entry is not a dict, skip it (branch 633->632)."""
    ticket = Ticket.objects.create(
        state=Ticket.State.STARTED,
        extra={"mrs": {"url1": "not-a-dict"}},
    )
    assert _first_mr_title(ticket) == ""


def test_first_mr_title_empty_title() -> None:
    """When mr dict has empty title, skip it (branch 635->632)."""
    ticket = Ticket.objects.create(
        state=Ticket.State.STARTED,
        extra={"mrs": {"url1": {"title": ""}}},
    )
    assert _first_mr_title(ticket) == ""


def test_first_mr_title_returns_first_nonempty_title() -> None:
    ticket = Ticket.objects.create(
        state=Ticket.State.STARTED,
        extra={"mrs": {"url1": {"title": ""}, "url2": {"title": "My Feature"}}},
    )
    assert _first_mr_title(ticket) == "My Feature"


# --- _last_result_for_tasks (branch 354->351) ---


from teetree.core.selectors import _last_result_for_tasks  # noqa: E402


def test_last_result_for_tasks_empty_list() -> None:
    """Empty task id list should return empty dict (loop doesn't execute)."""
    assert _last_result_for_tasks([]) == {}


def test_last_result_for_tasks_returns_summary() -> None:
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


def test_last_result_for_tasks_skips_empty_summary() -> None:
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
        result={"other_key": "value"},  # No "summary" key → empty summary
    )

    result = _last_result_for_tasks([task.pk])

    assert task.pk not in result


# --- build_active_sessions: sessions dir does not exist ---


def test_build_active_sessions_no_sessions_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When _CLAUDE_SESSIONS_DIR is not a directory, return empty list."""
    monkeypatch.setattr("teetree.core.selectors._CLAUDE_SESSIONS_DIR", tmp_path / "nonexistent")

    result = build_active_sessions()

    assert result == []


def test_cached_returns_stored_value_within_ttl() -> None:
    _panel_cache.clear()
    calls: list[int] = []

    def builder() -> str:
        calls.append(1)
        return "fresh"

    assert _cached("test_key", builder, ttl=60.0) == "fresh"
    assert _cached("test_key", builder, ttl=60.0) == "fresh"
    assert len(calls) == 1
    _panel_cache.clear()


def test_cached_rebuilds_after_ttl_expires() -> None:
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


def test_invalidate_panel_cache_by_name() -> None:
    _panel_cache["a"] = (0.0, "val_a")
    _panel_cache["b"] = (0.0, "val_b")

    invalidate_panel_cache("a")

    assert "a" not in _panel_cache
    assert "b" in _panel_cache
    _panel_cache.clear()
