from unittest.mock import patch

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from teetree.core.models import Session, Task, Ticket, Worktree
from teetree.core.sync import SyncResult
from teetree.core.views.dashboard import DashboardView, _panel_context

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _reset_dashboard_sync_flag():
    DashboardView._synced = False
    yield
    DashboardView._synced = False


@pytest.fixture
def _mock_perform_sync():
    with patch("teetree.core.views.dashboard.perform_sync"):
        yield


# ---------------------------------------------------------------------------
# Dashboard views
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_mock_perform_sync")
def test_dashboard_view_renders_full_page() -> None:
    ticket = Ticket.objects.create(
        issue_url="https://example.com/issues/301",
        variant="shipping",
        repos=["backend"],
        state=Ticket.State.STARTED,
    )
    session = Session.objects.create(ticket=ticket, agent_id="codex")
    Worktree.objects.create(
        ticket=ticket,
        repo_path="/tmp/backend",
        branch="feature/301",
        state=Worktree.State.READY,
    )
    Task.objects.create(
        ticket=ticket,
        session=session,
        execution_target=Task.ExecutionTarget.INTERACTIVE,
        execution_reason="Need product input",
    )

    response = Client().get(reverse("teetree:dashboard"))

    assert response.status_code == 200
    assert b"TeaTree Runtime" in response.content
    assert b"In-Flight Tickets" in response.content
    assert b"Action Required" in response.content
    assert b"hx-get" in response.content


def test_dashboard_panel_view_requires_htmx() -> None:
    response = Client().get(reverse("teetree:dashboard-panel", args=["summary"]))

    assert response.status_code == 404


def test_dashboard_panel_view_renders_requested_fragment() -> None:
    ticket = Ticket.objects.create(issue_url="https://example.com/issues/302", state=Ticket.State.CODED)
    session = Session.objects.create(ticket=ticket, agent_id="claude")
    Task.objects.create(ticket=ticket, session=session, execution_target=Task.ExecutionTarget.INTERACTIVE)

    client = Client()

    summary = client.get(reverse("teetree:dashboard-panel", args=["summary"]), HTTP_HX_REQUEST="true")
    tickets = client.get(reverse("teetree:dashboard-panel", args=["tickets"]), HTTP_HX_REQUEST="true")
    queue = client.get(reverse("teetree:dashboard-panel", args=["queue"]), HTTP_HX_REQUEST="true")

    assert summary.status_code == 200
    assert b"In Flight Tickets" in summary.content
    assert tickets.status_code == 200
    assert b"#302" in tickets.content
    assert queue.status_code == 200
    assert b"Launch" in queue.content


def test_dashboard_panel_view_rejects_unknown_panels() -> None:
    response = Client().get(reverse("teetree:dashboard-panel", args=["unknown"]), HTTP_HX_REQUEST="true")

    assert response.status_code == 404


def test_panel_context_raises_for_unknown_panel() -> None:
    with pytest.raises(ValueError, match="Unsupported panel"):
        _panel_context("unknown")


@pytest.mark.usefixtures("_mock_perform_sync")
def test_dashboard_renders_sync_button() -> None:
    response = Client().get(reverse("teetree:dashboard"))

    assert response.status_code == 200
    assert b"Sync All" in response.content
    assert b"hx-post" in response.content
    assert b"dashboard-sync" in response.content or b"/dashboard/sync/" in response.content


def test_dashboard_panel_action_required() -> None:
    """Cover the 'action_required' branch of _panel_context (line 81)."""
    response = Client().get(reverse("teetree:dashboard-panel", args=["action_required"]), HTTP_HX_REQUEST="true")

    assert response.status_code == 200


def test_dashboard_panel_worktrees() -> None:
    """Cover the 'worktrees' branch of _panel_context."""
    ticket = Ticket.objects.create(state=Ticket.State.STARTED)
    Worktree.objects.create(ticket=ticket, repo_path="/tmp/wt", branch="main", state=Worktree.State.READY)

    response = Client().get(reverse("teetree:dashboard-panel", args=["worktrees"]), HTTP_HX_REQUEST="true")

    assert response.status_code == 200


def test_dashboard_panel_headless_queue() -> None:
    """Cover the 'headless_queue' branch of _panel_context."""
    ticket = Ticket.objects.create(state=Ticket.State.STARTED)
    session = Session.objects.create(ticket=ticket)
    Task.objects.create(ticket=ticket, session=session, execution_target=Task.ExecutionTarget.HEADLESS)

    response = Client().get(reverse("teetree:dashboard-panel", args=["headless_queue"]), HTTP_HX_REQUEST="true")

    assert response.status_code == 200


def test_dashboard_panel_headless_queue_show_dismissed() -> None:
    """Cover the show_dismissed=True branch for headless_queue."""
    response = Client().get(
        reverse("teetree:dashboard-panel", args=["headless_queue"]),
        {"show_dismissed": "1"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200


def test_dashboard_panel_sessions() -> None:
    """Cover the 'sessions' branch of _panel_context."""
    response = Client().get(reverse("teetree:dashboard-panel", args=["sessions"]), HTTP_HX_REQUEST="true")

    assert response.status_code == 200


def test_dashboard_panel_review_comments() -> None:
    """Cover the 'review_comments' branch of _panel_context."""
    response = Client().get(reverse("teetree:dashboard-panel", args=["review_comments"]), HTTP_HX_REQUEST="true")

    assert response.status_code == 200


def test_dashboard_panel_activity() -> None:
    """Cover the 'activity' branch of _panel_context."""
    response = Client().get(reverse("teetree:dashboard-panel", args=["activity"]), HTTP_HX_REQUEST="true")

    assert response.status_code == 200


def test_task_detail_returns_200_for_existing_task() -> None:
    """Cover TaskDetailView when the task exists."""
    ticket = Ticket.objects.create(state=Ticket.State.STARTED)
    session = Session.objects.create(ticket=ticket, agent_id="test")
    task = Task.objects.create(ticket=ticket, session=session, phase="coding")

    response = Client().get(
        reverse("teetree:task-detail", args=[task.pk]),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200


def test_task_detail_returns_404_for_missing_task() -> None:
    """Cover TaskDetailView when build_task_detail returns None."""
    response = Client().get(
        reverse("teetree:task-detail", args=[999999]),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Sync / action views
# ---------------------------------------------------------------------------


def test_sync_endpoint_triggers_sync_and_returns_html(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "teetree.core.views.actions.perform_sync",
        lambda: SyncResult(mrs_found=3, tickets_created=1, tickets_updated=2),
    )

    response = Client().post(reverse("teetree:dashboard-sync"))

    assert response.status_code == 200
    assert b"Synced 3 MRs" in response.content
    assert b"1 new" in response.content


def test_sync_endpoint_shows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "teetree.core.views.actions.perform_sync",
        lambda: SyncResult(errors=["TEATREE_GITLAB_TOKEN is not set"]),
    )

    response = Client().post(reverse("teetree:dashboard-sync"))

    assert response.status_code == 200
    assert b"Sync error" in response.content
    assert b"TEATREE_GITLAB_TOKEN" in response.content


# ---------------------------------------------------------------------------
# CancelTaskView
# ---------------------------------------------------------------------------


def test_cancel_pending_task_returns_failed_status() -> None:
    ticket = Ticket.objects.create(state=Ticket.State.STARTED)
    session = Session.objects.create(ticket=ticket, agent_id="agent")
    task = Task.objects.create(
        ticket=ticket,
        session=session,
        execution_target=Task.ExecutionTarget.HEADLESS,
        status=Task.Status.PENDING,
    )

    response = Client().post(reverse("teetree:task-cancel", args=[task.pk]))

    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == task.pk
    assert data["status"] == "failed"


def test_cancel_completed_task_returns_409() -> None:
    ticket = Ticket.objects.create(state=Ticket.State.STARTED)
    session = Session.objects.create(ticket=ticket, agent_id="agent")
    task = Task.objects.create(
        ticket=ticket,
        session=session,
        execution_target=Task.ExecutionTarget.HEADLESS,
        status=Task.Status.COMPLETED,
    )

    response = Client().post(reverse("teetree:task-cancel", args=[task.pk]))

    assert response.status_code == 409
    assert response.json()["error"] == "Task already finished"


def test_cancel_nonexistent_task_returns_404() -> None:
    response = Client().post(reverse("teetree:task-cancel", args=[999999]))

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# TicketTransitionView
# ---------------------------------------------------------------------------


def test_ticket_transition_scope_succeeds() -> None:
    ticket = Ticket.objects.create(state=Ticket.State.NOT_STARTED)

    response = Client().post(
        reverse("teetree:ticket-transition", args=[ticket.pk]),
        {"transition": "scope"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ticket_id"] == ticket.pk
    assert data["state"] == "Scoped"


def test_ticket_transition_unknown_transition_returns_400() -> None:
    ticket = Ticket.objects.create(state=Ticket.State.NOT_STARTED)

    response = Client().post(
        reverse("teetree:ticket-transition", args=[ticket.pk]),
        {"transition": "nonexistent"},
    )

    assert response.status_code == 400
    assert "Unknown transition" in response.json()["error"]


def test_ticket_transition_missing_ticket_returns_404() -> None:
    response = Client().post(
        reverse("teetree:ticket-transition", args=[999999]),
        {"transition": "scope"},
    )

    assert response.status_code == 404


def test_ticket_transition_not_allowed_returns_409() -> None:
    """Try to call 'start' on a NOT_STARTED ticket (requires SCOPED)."""
    ticket = Ticket.objects.create(state=Ticket.State.NOT_STARTED)

    response = Client().post(
        reverse("teetree:ticket-transition", args=[ticket.pk]),
        {"transition": "start"},
    )

    assert response.status_code == 409
    assert "not allowed" in response.json()["error"]


def test_ticket_transition_empty_transition_returns_400() -> None:
    """Empty transition should be rejected as unknown."""
    ticket = Ticket.objects.create(state=Ticket.State.NOT_STARTED)

    response = Client().post(
        reverse("teetree:ticket-transition", args=[ticket.pk]),
    )

    assert response.status_code == 400


def test_ticket_transition_invalid_method_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """If an allowed transition name doesn't map to a method on the ticket, returns 400."""
    ticket = Ticket.objects.create(state=Ticket.State.NOT_STARTED)

    # Temporarily add a fake transition to the allowed set to trigger the method is None branch
    from teetree.core.views import actions  # noqa: PLC0415

    original_set = actions._ALLOWED_TRANSITIONS
    monkeypatch.setattr(actions, "_ALLOWED_TRANSITIONS", original_set | {"nonexistent_method"})

    response = Client().post(
        reverse("teetree:ticket-transition", args=[ticket.pk]),
        {"transition": "nonexistent_method"},
    )

    assert response.status_code == 400
    assert "Invalid transition" in response.json()["error"]


# ---------------------------------------------------------------------------
# CreateTaskView
# ---------------------------------------------------------------------------


@override_settings(
    TASKS={
        "default": {
            "BACKEND": "django_tasks.backends.immediate.ImmediateBackend",
        },
    },
    TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
    TEATREE_HEADLESS_RUNTIME="claude-code",
)
def test_create_task_headless_creates_and_enqueues(monkeypatch: pytest.MonkeyPatch) -> None:
    """CreateTaskView with headless target claims and enqueues the task."""
    monkeypatch.setattr("teetree.agents.headless.shutil.which", lambda _name: "/usr/bin/claude-code")
    monkeypatch.setattr(
        "teetree.agents.headless.subprocess.run",
        lambda *_a, **_kw: __import__("subprocess").CompletedProcess([], 0, '{"summary": "OK"}', ""),
    )

    ticket = Ticket.objects.create(state=Ticket.State.STARTED)

    response = Client().post(
        reverse("teetree:ticket-create-task", args=[ticket.pk]),
        {"phase": "coding", "target": Task.ExecutionTarget.HEADLESS},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] is not None
    # A new session should have been created since there was none
    assert Session.objects.filter(ticket=ticket).exists()


def test_create_task_interactive_creates_without_enqueue() -> None:
    """CreateTaskView with interactive target creates task but does not enqueue."""
    ticket = Ticket.objects.create(state=Ticket.State.STARTED)
    session = Session.objects.create(ticket=ticket, agent_id="existing")

    response = Client().post(
        reverse("teetree:ticket-create-task", args=[ticket.pk]),
        {"phase": "reviewing", "target": Task.ExecutionTarget.INTERACTIVE},
    )

    assert response.status_code == 200
    data = response.json()
    task = Task.objects.get(pk=data["task_id"])
    assert task.execution_target == Task.ExecutionTarget.INTERACTIVE
    assert task.session == session  # reused existing session


def test_create_task_missing_ticket_returns_404() -> None:
    response = Client().post(
        reverse("teetree:ticket-create-task", args=[999999]),
    )

    assert response.status_code == 404


def test_create_task_creates_session_when_none_exists() -> None:
    """When the ticket has no session, CreateTaskView creates one with agent_id='dashboard'."""
    ticket = Ticket.objects.create(state=Ticket.State.STARTED)
    assert not Session.objects.filter(ticket=ticket).exists()

    response = Client().post(
        reverse("teetree:ticket-create-task", args=[ticket.pk]),
        {"target": Task.ExecutionTarget.INTERACTIVE},
    )

    assert response.status_code == 200
    session = Session.objects.get(ticket=ticket)
    assert session.agent_id == "dashboard"


def test_create_task_headless_already_claimed_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """When claim() raises InvalidTransitionError, returns 409."""
    from teetree.core.models import InvalidTransitionError  # noqa: PLC0415

    ticket = Ticket.objects.create(state=Ticket.State.STARTED)
    Session.objects.create(ticket=ticket, agent_id="agent")

    def _raise_claimed(self: object, *, claimed_by: str, **_kw: object) -> None:
        msg = "Task already claimed"
        raise InvalidTransitionError(msg)

    monkeypatch.setattr(Task, "claim", _raise_claimed)

    response = Client().post(
        reverse("teetree:ticket-create-task", args=[ticket.pk]),
        {"phase": "coding", "target": Task.ExecutionTarget.HEADLESS},
    )

    assert response.status_code == 409
    assert response.json()["error"] == "Task already claimed"


@override_settings(TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay")
def test_create_task_headless_enqueue_failure_fails_task(monkeypatch: pytest.MonkeyPatch) -> None:
    ticket = Ticket.objects.create(state=Ticket.State.STARTED)
    Session.objects.create(ticket=ticket, agent_id="agent")

    class BrokenTask:
        @staticmethod
        def enqueue(*_args: object, **_kwargs: object) -> None:
            msg = "queue unavailable"
            raise RuntimeError(msg)

    monkeypatch.setattr("teetree.core.tasks.execute_headless_task", BrokenTask)

    response = Client().post(
        reverse("teetree:ticket-create-task", args=[ticket.pk]),
        {"phase": "coding", "target": Task.ExecutionTarget.HEADLESS},
    )

    assert response.status_code == 500
    assert response.json()["error"] == "queue unavailable"

    task = Task.objects.get(ticket=ticket, phase="coding")
    assert task.status == Task.Status.FAILED
