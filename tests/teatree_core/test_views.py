import os
import subprocess as subprocess_mod
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache as django_cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

import teatree.agents.headless as headless_mod
import teatree.core.overlay_loader as overlay_loader_mod
import teatree.core.tasks as tasks_mod
import teatree.core.views.actions as actions_views
import teatree.utils.run as utils_run_mod
from teatree.config import OverlayEntry
from teatree.core.models import Session, Task, Ticket, Worktree
from teatree.core.sync import PENDING_REVIEWS_CACHE_KEY, SyncResult
from teatree.core.views.dashboard import _build_overlay_branches, _panel_context
from tests.teatree_core.conftest import CommandOverlay

_MOCK_OVERLAY = {"test": CommandOverlay()}


class _MockDjangoTask:
    @staticmethod
    def enqueue(*_args: object, **_kwargs: object) -> None:
        pass


# ---------------------------------------------------------------------------
# DashboardView
# ---------------------------------------------------------------------------


class TestDashboardView(TestCase):
    def test_renders_full_page(self) -> None:
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/301",
            variant="shipping",
            repos=["backend"],
            state=Ticket.State.STARTED,
        )
        session = Session.objects.create(ticket=ticket, overlay="test", agent_id="codex")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
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

        response = Client().get(reverse("teatree:dashboard"))

        assert response.status_code == 200
        assert b"TeaTree Dashboard" in response.content
        assert b"In-Flight Tickets" in response.content
        assert b"Action Required" in response.content
        assert b"hx-get" in response.content

    def test_renders_with_overlay_param(self) -> None:
        response = Client().get(reverse("teatree:dashboard") + "?overlay=test")
        assert response.status_code == 200
        assert b"TeaTree Dashboard" in response.content

    def test_renders_with_known_overlay_uses_overlay_logo(self) -> None:
        overlay = CommandOverlay()
        overlay.config = type(overlay.config)()
        overlay.config.dashboard_logo = "/static/custom-logo.svg"
        mock_overlays = {"test": overlay}
        with patch("teatree.core.views.dashboard.get_all_overlays", return_value=mock_overlays):
            response = Client().get(reverse("teatree:dashboard") + "?overlay=test")
        assert response.status_code == 200
        assert response.context["logo_url"] == "/static/custom-logo.svg"

    def test_renders_with_known_overlay_falls_back_to_default_logo(self) -> None:
        with patch("teatree.core.views.dashboard.get_all_overlays", return_value=_MOCK_OVERLAY):
            response = Client().get(reverse("teatree:dashboard") + "?overlay=test")
        assert response.status_code == 200
        assert "teatree-logo.jpg" in response.context["logo_url"]

    def test_handles_git_command_failure(self) -> None:
        with patch("teatree.utils.git.run", side_effect=FileNotFoundError("git not found")):
            response = Client().get(reverse("teatree:dashboard"))
        assert response.status_code == 200
        assert response.context["git_sha"] == ""

    def test_renders_sync_button(self) -> None:
        response = Client().get(reverse("teatree:dashboard"))

        assert response.status_code == 200
        assert b"Sync All" in response.content
        assert b"hx-post" in response.content
        assert b"dashboard-sync" in response.content or b"/dashboard/sync/" in response.content


# ---------------------------------------------------------------------------
# DashboardPanelView
# ---------------------------------------------------------------------------


class TestDashboardPanelView(TestCase):
    def test_requires_htmx(self) -> None:
        response = Client().get(reverse("teatree:dashboard-panel", args=["summary"]))

        assert response.status_code == 404

    def test_renders_requested_fragment(self) -> None:
        ticket = Ticket.objects.create(
            overlay="test", issue_url="https://example.com/issues/302", state=Ticket.State.CODED
        )
        session = Session.objects.create(ticket=ticket, overlay="test", agent_id="claude")
        Task.objects.create(ticket=ticket, session=session, execution_target=Task.ExecutionTarget.INTERACTIVE)

        client = Client()

        summary = client.get(reverse("teatree:dashboard-panel", args=["summary"]), HTTP_HX_REQUEST="true")
        tickets = client.get(reverse("teatree:dashboard-panel", args=["tickets"]), HTTP_HX_REQUEST="true")
        queue = client.get(reverse("teatree:dashboard-panel", args=["queue"]), HTTP_HX_REQUEST="true")

        assert summary.status_code == 200
        assert b"In Flight Tickets" in summary.content
        assert tickets.status_code == 200
        assert b"#302" in tickets.content
        assert queue.status_code == 200
        assert b"Launch" in queue.content

    def test_rejects_unknown_panels(self) -> None:
        response = Client().get(reverse("teatree:dashboard-panel", args=["unknown"]), HTTP_HX_REQUEST="true")

        assert response.status_code == 404

    def test_action_required(self) -> None:
        """Cover the 'action_required' branch of _panel_context (line 81)."""
        response = Client().get(reverse("teatree:dashboard-panel", args=["action_required"]), HTTP_HX_REQUEST="true")

        assert response.status_code == 200

    def test_worktrees(self) -> None:
        """Cover the 'worktrees' branch of _panel_context."""
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        Worktree.objects.create(
            ticket=ticket, overlay="test", repo_path="/tmp/wt", branch="main", state=Worktree.State.READY
        )

        response = Client().get(reverse("teatree:dashboard-panel", args=["worktrees"]), HTTP_HX_REQUEST="true")

        assert response.status_code == 200

    def test_headless_queue(self) -> None:
        """Cover the 'headless_queue' branch of _panel_context."""
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, overlay="test")
        Task.objects.create(ticket=ticket, session=session, execution_target=Task.ExecutionTarget.HEADLESS)

        response = Client().get(reverse("teatree:dashboard-panel", args=["headless_queue"]), HTTP_HX_REQUEST="true")

        assert response.status_code == 200

    def test_headless_queue_show_dismissed(self) -> None:
        """Cover the show_dismissed=True branch for headless_queue."""
        response = Client().get(
            reverse("teatree:dashboard-panel", args=["headless_queue"]),
            {"show_dismissed": "1"},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200

    def test_sessions(self) -> None:
        """Cover the 'sessions' branch of _panel_context."""
        response = Client().get(reverse("teatree:dashboard-panel", args=["sessions"]), HTTP_HX_REQUEST="true")

        assert response.status_code == 200

    def test_review_comments_panel_removed(self) -> None:
        """review_comments panel was merged into action_required — returns 404."""
        response = Client().get(reverse("teatree:dashboard-panel", args=["review_comments"]), HTTP_HX_REQUEST="true")

        assert response.status_code == 404

    def test_automation(self) -> None:
        response = Client().get(reverse("teatree:dashboard-panel", args=["automation"]), HTTP_HX_REQUEST="true")

        assert response.status_code == 200

    def test_activity(self) -> None:
        """Cover the 'activity' branch of _panel_context."""
        response = Client().get(reverse("teatree:dashboard-panel", args=["activity"]), HTTP_HX_REQUEST="true")

        assert response.status_code == 200

    def test_pending_reviews_empty(self) -> None:
        """Pending reviews panel renders even when cache is empty."""
        response = Client().get(reverse("teatree:dashboard-panel", args=["pending_reviews"]), HTTP_HX_REQUEST="true")

        assert response.status_code == 200
        assert b"No pending reviews" in response.content

    def test_pending_reviews_with_cached_data(self) -> None:
        """Pending reviews panel renders cached review data."""
        django_cache.set(
            PENDING_REVIEWS_CACHE_KEY,
            [
                {
                    "url": "https://github.com/org/repo/pull/42",
                    "title": "Fix the thing",
                    "repo": "repo",
                    "iid": "42",
                    "author": "alice",
                    "draft": "False",
                    "updated_at": "2026-04-09T12:00:00Z",
                },
            ],
        )

        response = Client().get(reverse("teatree:dashboard-panel", args=["pending_reviews"]), HTTP_HX_REQUEST="true")

        assert response.status_code == 200
        assert b"repo" in response.content
        assert b"#42" in response.content
        assert b"alice" in response.content

        django_cache.delete(PENDING_REVIEWS_CACHE_KEY)


# ---------------------------------------------------------------------------
# _panel_context
# ---------------------------------------------------------------------------


class TestOverlaySelector(TestCase):
    def test_dashboard_passes_overlay_list_to_template(self) -> None:
        response = Client().get(reverse("teatree:dashboard"))

        assert response.status_code == 200
        assert "overlays" in response.context
        assert "selected_overlay" in response.context

    def test_dashboard_with_overlay_param_filters_snapshot(self) -> None:
        Ticket.objects.create(overlay="alpha", state=Ticket.State.STARTED)
        Ticket.objects.create(overlay="beta", state=Ticket.State.STARTED)

        response = Client().get(reverse("teatree:dashboard"), {"overlay": "alpha"})

        assert response.status_code == 200
        assert response.context["selected_overlay"] == "alpha"
        assert response.context["snapshot"].summary.in_flight_tickets == 1

    def test_panel_view_passes_overlay_to_builders(self) -> None:
        Ticket.objects.create(
            overlay="alpha", state=Ticket.State.STARTED, issue_url="https://gitlab.com/o/r/-/issues/1"
        )
        Ticket.objects.create(overlay="beta", state=Ticket.State.STARTED, issue_url="https://gitlab.com/o/r/-/issues/2")

        response = Client().get(
            reverse("teatree:dashboard-panel", args=["tickets"]),
            {"overlay": "alpha"},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200
        assert len(response.context["tickets"]) == 1


class TestBuildOverlayBranches:
    def test_returns_branch_for_overlay_with_project_path(self) -> None:
        entries = [OverlayEntry(name="my-overlay", overlay_class="", project_path=Path("/opt/my-overlay"))]
        with (
            patch("teatree.config.discover_overlays", return_value=entries),
            patch("teatree.utils.git.current_branch", return_value="main"),
        ):
            result = _build_overlay_branches({})

        assert result == {"my-overlay": "main"}

    def test_returns_unknown_when_git_returns_empty(self) -> None:
        entries = [OverlayEntry(name="broken", overlay_class="", project_path=Path("/nonexistent"))]
        with (
            patch("teatree.config.discover_overlays", return_value=entries),
            patch("teatree.utils.git.current_branch", return_value=""),
        ):
            result = _build_overlay_branches({})

        assert result == {"broken": "unknown"}

    def test_returns_unknown_when_git_not_found(self) -> None:
        entries = [OverlayEntry(name="no-git", overlay_class="", project_path=Path("/opt/repo"))]
        with (
            patch("teatree.config.discover_overlays", return_value=entries),
            patch("teatree.utils.git.current_branch", side_effect=FileNotFoundError("git not found")),
        ):
            result = _build_overlay_branches({})

        assert result == {"no-git": "unknown"}

    def test_resolves_branch_from_module_when_no_project_path(self) -> None:
        overlay = CommandOverlay()
        entries = [OverlayEntry(name="test", overlay_class="", project_path=None)]
        with (
            patch("teatree.config.discover_overlays", return_value=entries),
            patch("teatree.utils.git.current_branch", return_value="feature-branch"),
        ):
            result = _build_overlay_branches({"test": overlay})

        assert result["test"] == "feature-branch"


class TestPanelContext(TestCase):
    def test_raises_for_unknown_panel(self) -> None:
        with pytest.raises(ValueError, match="Unsupported panel"):
            _panel_context("unknown")


# ---------------------------------------------------------------------------
# TaskDetailView
# ---------------------------------------------------------------------------


class TestTaskDetailView(TestCase):
    def test_returns_200_for_existing_task(self) -> None:
        """Cover TaskDetailView when the task exists."""
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, overlay="test", agent_id="test")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")

        response = Client().get(
            reverse("teatree:task-detail", args=[task.pk]),
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 200

    def test_returns_404_for_missing_task(self) -> None:
        """Cover TaskDetailView when build_task_detail returns None."""
        response = Client().get(
            reverse("teatree:task-detail", args=[999999]),
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# SyncFollowupView
# ---------------------------------------------------------------------------


class TestSyncFollowupView(TestCase):
    def test_triggers_sync_and_returns_html(self) -> None:
        with patch.object(
            actions_views,
            "perform_sync",
            return_value=SyncResult(mrs_found=3, tickets_created=1, tickets_updated=2),
        ):
            response = Client().post(reverse("teatree:dashboard-sync"))

        assert response.status_code == 200
        assert b"Synced 3 PRs" in response.content
        assert b"1 new" in response.content

    def test_shows_errors(self) -> None:
        with patch.object(
            actions_views,
            "perform_sync",
            return_value=SyncResult(errors=["No code host token for test"]),
        ):
            response = Client().post(reverse("teatree:dashboard-sync"))

        assert response.status_code == 200
        assert b"Sync error" in response.content
        assert b"No code host token for" in response.content


# ---------------------------------------------------------------------------
# CancelTaskView
# ---------------------------------------------------------------------------


class TestCancelTaskView(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        cls.session = Session.objects.create(ticket=cls.ticket, overlay="test", agent_id="agent")

    def test_pending_task_returns_failed_status(self) -> None:
        task = Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.PENDING,
        )

        response = Client().post(reverse("teatree:task-cancel", args=[task.pk]))

        assert response.status_code == 200
        data = response.json()
        assert data["task_id"] == task.pk
        assert data["status"] == "failed"

    def test_completed_task_returns_409(self) -> None:
        task = Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.COMPLETED,
        )

        response = Client().post(reverse("teatree:task-cancel", args=[task.pk]))

        assert response.status_code == 409
        assert response.json()["error"] == "Task already finished"

    def test_claimed_task_without_confirm_returns_409(self) -> None:
        task = Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.CLAIMED,
        )

        response = Client().post(reverse("teatree:task-cancel", args=[task.pk]))

        assert response.status_code == 409
        assert "in progress" in response.json()["error"]

    def test_claimed_task_with_confirm_cancels(self) -> None:
        task = Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.CLAIMED,
        )

        response = Client().post(reverse("teatree:task-cancel", args=[task.pk]), {"confirm": "true"})

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "failed"

    def test_nonexistent_task_returns_404(self) -> None:
        response = Client().post(reverse("teatree:task-cancel", args=[999999]))

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# ReopenTaskView
# ---------------------------------------------------------------------------


class TestReopenTaskView(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        cls.session = Session.objects.create(ticket=cls.ticket, overlay="test", agent_id="agent")

    def test_reopen_failed_task_returns_pending(self) -> None:
        task = Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.FAILED,
        )

        response = Client().post(reverse("teatree:task-reopen", args=[task.pk]))

        assert response.status_code == 200
        data = response.json()
        assert data["task_id"] == task.pk
        assert data["status"] == "pending"
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_reopen_pending_task_returns_409(self) -> None:
        task = Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.PENDING,
        )

        response = Client().post(reverse("teatree:task-reopen", args=[task.pk]))

        assert response.status_code == 409

    def test_reopen_completed_task_returns_409(self) -> None:
        task = Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.COMPLETED,
        )

        response = Client().post(reverse("teatree:task-reopen", args=[task.pk]))

        assert response.status_code == 409

    def test_nonexistent_task_returns_404(self) -> None:
        response = Client().post(reverse("teatree:task-reopen", args=[999999]))

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# TicketTransitionView
# ---------------------------------------------------------------------------


class TestTicketTransitionView(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create(overlay="test", state=Ticket.State.NOT_STARTED)

    def test_scope_succeeds(self) -> None:
        response = Client().post(
            reverse("teatree:ticket-transition", args=[self.ticket.pk]),
            {"transition": "scope"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["ticket_id"] == self.ticket.pk
        assert data["state"] == "Scoped"

    def test_unknown_transition_returns_400(self) -> None:
        response = Client().post(
            reverse("teatree:ticket-transition", args=[self.ticket.pk]),
            {"transition": "nonexistent"},
        )

        assert response.status_code == 400
        assert "Unknown transition" in response.json()["error"]

    def test_missing_ticket_returns_404(self) -> None:
        response = Client().post(
            reverse("teatree:ticket-transition", args=[999999]),
            {"transition": "scope"},
        )

        assert response.status_code == 404

    def test_not_allowed_returns_409(self) -> None:
        """Try to call 'start' on a NOT_STARTED ticket (requires SCOPED)."""
        response = Client().post(
            reverse("teatree:ticket-transition", args=[self.ticket.pk]),
            {"transition": "start"},
        )

        assert response.status_code == 409
        assert "not allowed" in response.json()["error"]

    def test_empty_transition_returns_400(self) -> None:
        """Empty transition should be rejected as unknown."""
        response = Client().post(
            reverse("teatree:ticket-transition", args=[self.ticket.pk]),
        )

        assert response.status_code == 400

    def test_invalid_method_returns_400(self) -> None:
        """If an allowed transition name doesn't map to a method on the ticket, returns 400."""
        from teatree.core.views import actions  # noqa: PLC0415

        # Temporarily add a fake transition to the allowed set to trigger the method is None branch
        with patch.object(actions, "_ALLOWED_TRANSITIONS", actions._ALLOWED_TRANSITIONS | {"nonexistent_method"}):
            response = Client().post(
                reverse("teatree:ticket-transition", args=[self.ticket.pk]),
                {"transition": "nonexistent_method"},
            )

        assert response.status_code == 400
        assert "Invalid transition" in response.json()["error"]

    def test_ignore_hides_ticket(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        response = Client().post(
            reverse("teatree:ticket-transition", args=[ticket.pk]),
            {"transition": "ignore"},
        )

        assert response.status_code == 200
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IGNORED

    def test_unignore_restores_ticket(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IGNORED, extra={"ignored_from": "coded"})
        response = Client().post(
            reverse("teatree:ticket-transition", args=[ticket.pk]),
            {"transition": "unignore"},
        )

        assert response.status_code == 200
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.CODED


# ---------------------------------------------------------------------------
# CreateTaskView
# ---------------------------------------------------------------------------


class TestCreateTaskView(TestCase):
    @override_settings(
        TASKS={
            "default": {
                "BACKEND": "django_tasks.backends.immediate.ImmediateBackend",
            },
        },
    )
    def test_headless_creates_and_enqueues(self) -> None:
        """CreateTaskView with headless target claims and enqueues the task."""
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)

        with (
            patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude-code"),
            patch.object(
                utils_run_mod.subprocess,
                "run",
                return_value=__import__("subprocess").CompletedProcess([], 0, '{"summary": "OK"}', ""),
            ),
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
        ):
            response = Client().post(
                reverse("teatree:ticket-create-task", args=[ticket.pk]),
                {"phase": "coding", "target": Task.ExecutionTarget.HEADLESS},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["task_id"] is not None
        # A new session should have been created since there was none
        assert Session.objects.filter(ticket=ticket).exists()

    def test_interactive_creates_without_enqueue(self) -> None:
        """CreateTaskView with interactive target creates task but does not enqueue."""
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, overlay="test", agent_id="existing")

        response = Client().post(
            reverse("teatree:ticket-create-task", args=[ticket.pk]),
            {"phase": "reviewing", "target": Task.ExecutionTarget.INTERACTIVE},
        )

        assert response.status_code == 200
        data = response.json()
        task = Task.objects.get(pk=data["task_id"])
        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE
        assert task.session == session  # reused existing session

    def test_missing_ticket_returns_404(self) -> None:
        response = Client().post(
            reverse("teatree:ticket-create-task", args=[999999]),
        )

        assert response.status_code == 404

    def test_creates_session_when_none_exists(self) -> None:
        """When the ticket has no session, CreateTaskView creates one with agent_id='dashboard'."""
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        assert not Session.objects.filter(ticket=ticket).exists()

        response = Client().post(
            reverse("teatree:ticket-create-task", args=[ticket.pk]),
            {"target": Task.ExecutionTarget.INTERACTIVE},
        )

        assert response.status_code == 200
        session = Session.objects.get(ticket=ticket)
        assert session.agent_id == "dashboard"

    def test_headless_enqueues_without_claiming(self) -> None:
        """Headless tasks are enqueued without immediate claim — worker claims on pickup."""
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        Session.objects.create(ticket=ticket, overlay="test", agent_id="agent")

        with patch.object(tasks_mod, "execute_headless_task", _MockDjangoTask):
            response = Client().post(
                reverse("teatree:ticket-create-task", args=[ticket.pk]),
                {"phase": "coding", "target": Task.ExecutionTarget.HEADLESS},
            )

        assert response.status_code == 200
        task = Task.objects.latest("pk")
        assert task.status == Task.Status.PENDING  # Not claimed until worker picks it up

    def test_headless_enqueue_failure_leaves_task_pending(self) -> None:
        """If the auto-enqueue signal fails, the task stays PENDING for drain to retry."""
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        Session.objects.create(ticket=ticket, overlay="test", agent_id="agent")

        class BrokenTask:
            @staticmethod
            def enqueue(*_args: object, **_kwargs: object) -> None:
                msg = "queue unavailable"
                raise RuntimeError(msg)

        with (
            patch.object(tasks_mod, "execute_headless_task", BrokenTask),
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
        ):
            response = Client().post(
                reverse("teatree:ticket-create-task", args=[ticket.pk]),
                {"phase": "coding", "target": Task.ExecutionTarget.HEADLESS},
            )

        assert response.status_code == 200

        task = Task.objects.get(ticket=ticket, phase="coding")
        assert task.status == Task.Status.PENDING

    def test_interactive_with_terminal_mode_auto_launches(self) -> None:
        """When terminal_mode is provided, interactive task is created, claimed, and launched."""
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        Session.objects.create(ticket=ticket, overlay="test", agent_id="agent")

        mock_attempt = type("Attempt", (), {"launch_url": "http://localhost:7681", "pk": 42})()
        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.agents.web_terminal.launch_web_session", return_value=mock_attempt),
        ):
            response = Client().post(
                reverse("teatree:ticket-create-task", args=[ticket.pk]),
                {
                    "target": Task.ExecutionTarget.INTERACTIVE,
                    "terminal_mode": "ttyd",
                    "terminal_app": "iterm2",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["launch_url"] == "http://localhost:7681"

        task = Task.objects.latest("pk")
        assert task.status == Task.Status.CLAIMED
        assert task.claimed_by == "dashboard-auto-launch"

    def test_interactive_without_terminal_mode_no_auto_launch(self) -> None:
        """Without terminal_mode, interactive task is created but NOT auto-launched."""
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)

        response = Client().post(
            reverse("teatree:ticket-create-task", args=[ticket.pk]),
            {"target": Task.ExecutionTarget.INTERACTIVE},
        )

        assert response.status_code == 200
        data = response.json()
        assert "launch_url" not in data

        task = Task.objects.get(pk=data["task_id"])
        assert task.status == Task.Status.PENDING

    def test_auto_launch_conflict_returns_409(self) -> None:
        """When task is already completed, auto-launch claim fails with 409."""
        from teatree.core.views.actions import CreateTaskView  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            status=Task.Status.COMPLETED,
        )

        result = CreateTaskView._auto_launch(task, "ttyd", "")
        assert result.status_code == 409

        import json  # noqa: PLC0415

        data = json.loads(result.content)
        assert "error" in data

    def test_auto_launch_exception_returns_500(self) -> None:
        """When launch_interactive_task raises, returns 500 and marks task failed."""
        from teatree.core.views.actions import CreateTaskView  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )

        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
            patch(
                "teatree.core.views.launch.launch_interactive_task",
                side_effect=RuntimeError("boom"),
            ),
        ):
            result = CreateTaskView._auto_launch(task, "ttyd", "")

        assert result.status_code == 500
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED


# ---------------------------------------------------------------------------
# TicketLifecycleView
# ---------------------------------------------------------------------------


class TestTicketLifecycleView(TestCase):
    def test_returns_mermaid_for_ticket_with_transitions(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope(issue_url="https://example.com/issues/view-1")
        ticket.save()

        response = Client().get(reverse("teatree:ticket-lifecycle", args=[ticket.pk]))

        assert response.status_code == 200
        content = response.content.decode()
        assert "stateDiagram-v2" in content
        assert "not_started --&gt; scoped" in content

    def test_returns_empty_for_ticket_without_transitions(self) -> None:
        ticket = Ticket.objects.create()

        response = Client().get(reverse("teatree:ticket-lifecycle", args=[ticket.pk]))

        assert response.status_code == 200
        content = response.content.decode()
        assert "note right of not_started" in content

    def test_returns_404_for_missing_ticket(self) -> None:
        response = Client().get(reverse("teatree:ticket-lifecycle", args=[999999]))

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# TaskGraphView
# ---------------------------------------------------------------------------


class TestTaskGraphView(TestCase):
    def test_returns_graph_for_ticket_with_tasks(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        Task.objects.create(ticket=ticket, session=session, phase="coding")

        response = Client().get(reverse("teatree:task-graph", args=[ticket.pk]))

        assert response.status_code == 200
        assert b"coding" in response.content

    def test_returns_empty_for_ticket_without_tasks(self) -> None:
        ticket = Ticket.objects.create()

        response = Client().get(reverse("teatree:task-graph", args=[ticket.pk]))

        assert response.status_code == 200
        assert b"No tasks" in response.content

    def test_returns_404_for_missing_ticket(self) -> None:
        response = Client().get(reverse("teatree:task-graph", args=[999999]))

        assert response.status_code == 404


class TestGitPullRepo:
    """Tests for _git_pull_repo helper."""

    def test_success(self, tmp_path: Path) -> None:
        completed = subprocess_mod.CompletedProcess([], 0, "Already up to date.\n", "")
        with patch("teatree.utils.run.subprocess") as mock_sub:
            mock_sub.run.return_value = completed
            mock_sub.TimeoutExpired = subprocess_mod.TimeoutExpired
            result = actions_views._git_pull_repo(tmp_path)

        assert result["ok"] is True
        assert "Already up to date" in str(result["output"])

    def test_timeout(self, tmp_path: Path) -> None:
        with patch("teatree.utils.run.subprocess") as mock_sub:
            mock_sub.TimeoutExpired = subprocess_mod.TimeoutExpired
            mock_sub.run.side_effect = subprocess_mod.TimeoutExpired(["git"], 30)
            result = actions_views._git_pull_repo(tmp_path)

        assert result["ok"] is False
        assert "timed out" in str(result["error"])

    def test_merge_conflict_aborts(self, tmp_path: Path) -> None:
        fail = subprocess_mod.CompletedProcess([], 1, "", "CONFLICT (content): Merge conflict in f.py")
        abort_ok = subprocess_mod.CompletedProcess([], 0, "", "")
        with patch("teatree.utils.run.subprocess") as mock_sub:
            mock_sub.run.side_effect = [fail, abort_ok]
            mock_sub.TimeoutExpired = subprocess_mod.TimeoutExpired
            result = actions_views._git_pull_repo(tmp_path)

        assert result["ok"] is False
        assert result.get("conflict") is True
        assert "Merge conflict" in str(result["error"])

    def test_stale_branch_switches_to_main(self, tmp_path: Path) -> None:
        fail = subprocess_mod.CompletedProcess([], 1, "", "no tracking information")
        branch = subprocess_mod.CompletedProcess([], 0, "old-branch\n", "")
        switch = subprocess_mod.CompletedProcess([], 0, "", "")
        pull_ok = subprocess_mod.CompletedProcess([], 0, "Updating abc..def\n", "")
        delete = subprocess_mod.CompletedProcess([], 0, "", "")
        with patch("teatree.utils.run.subprocess") as mock_sub:
            mock_sub.run.side_effect = [fail, branch, switch, pull_ok, delete]
            mock_sub.TimeoutExpired = subprocess_mod.TimeoutExpired
            result = actions_views._git_pull_repo(tmp_path)

        assert result["ok"] is True
        assert "Switched to main" in str(result["output"])
        assert "deleted stale branch 'old-branch'" in str(result["output"])

    def test_generic_failure(self, tmp_path: Path) -> None:
        fail = subprocess_mod.CompletedProcess([], 128, "", "fatal: bad repo")
        with patch("teatree.utils.run.subprocess") as mock_sub:
            mock_sub.run.return_value = fail
            mock_sub.TimeoutExpired = subprocess_mod.TimeoutExpired
            result = actions_views._git_pull_repo(tmp_path)

        assert result["ok"] is False
        assert "fatal" in str(result["error"])


class TestGitPullView(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    def test_success_returns_results(self) -> None:
        with (
            patch.object(actions_views, "_get_t3_repo", return_value=self.tmp_path),
            patch.object(actions_views, "_find_overlay_repo_dirs", return_value=[]),
            patch.object(actions_views, "_git_pull_repo", return_value={"ok": True, "output": "Already up to date."}),
        ):
            response = Client().post(reverse("teatree:dashboard-git-pull"))

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert "teatree" in data["results"]

    def test_pulls_overlay_repos(self) -> None:
        overlay_dir = self.tmp_path / "overlay"
        overlay_dir.mkdir()
        results = [
            {"ok": True, "output": "teatree updated"},
            {"ok": True, "output": "overlay updated"},
        ]
        with (
            patch.object(actions_views, "_get_t3_repo", return_value=self.tmp_path),
            patch.object(actions_views, "_find_overlay_repo_dirs", return_value=[("my-overlay", overlay_dir)]),
            patch.object(actions_views, "_git_pull_repo", side_effect=results),
        ):
            response = Client().post(reverse("teatree:dashboard-git-pull"))

        assert response.status_code == 200
        data = response.json()
        assert "teatree" in data["results"]
        assert "my-overlay" in data["results"]

    def test_failure_returns_errors_and_creates_task(self) -> None:
        with (
            patch.object(actions_views, "_get_t3_repo", return_value=self.tmp_path),
            patch.object(actions_views, "_find_overlay_repo_dirs", return_value=[]),
            patch.object(actions_views, "_git_pull_repo", return_value={"ok": False, "error": "fatal: bad repo"}),
        ):
            response = Client().post(reverse("teatree:dashboard-git-pull"))

        assert response.status_code == 500
        data = response.json()
        assert "teatree" in data["errors"]
        task = Task.objects.get(phase="maintenance")
        assert "git pull failed" in task.execution_reason

    def test_skips_overlay_same_as_teatree(self) -> None:
        with (
            patch.object(actions_views, "_get_t3_repo", return_value=self.tmp_path),
            patch.object(actions_views, "_find_overlay_repo_dirs", return_value=[("teatree", self.tmp_path)]),
            patch.object(
                actions_views, "_git_pull_repo", return_value={"ok": True, "output": "up to date"}
            ) as mock_pull,
        ):
            Client().post(reverse("teatree:dashboard-git-pull"))

        mock_pull.assert_called_once()

    def test_missing_repo_returns_400(self) -> None:
        with patch.object(actions_views, "_get_t3_repo", return_value=None):
            response = Client().post(reverse("teatree:dashboard-git-pull"))

        assert response.status_code == 400


class TestFindOverlayRepoDirs:
    def test_finds_overlay_with_git_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        git_dir = tmp_path / "overlay-repo"
        git_dir.mkdir()
        (git_dir / ".git").mkdir()
        pkg_dir = git_dir / "src" / "my_overlay"
        pkg_dir.mkdir(parents=True)
        mod_file = pkg_dir / "__init__.py"
        mod_file.write_text("")

        import types  # noqa: PLC0415

        fake_mod = types.ModuleType("my_overlay")
        fake_mod.__file__ = str(mod_file)

        fake_overlay = MagicMock()
        type(fake_overlay).__module__ = "my_overlay"

        monkeypatch.setitem(__import__("sys").modules, "my_overlay", fake_mod)
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value={"test": fake_overlay}):
            results = actions_views._find_overlay_repo_dirs()

        assert len(results) == 1
        assert results[0] == ("test", git_dir)

    def test_returns_empty_on_error(self) -> None:
        with patch("teatree.core.overlay_loader.get_all_overlays", side_effect=RuntimeError("fail")):
            assert actions_views._find_overlay_repo_dirs() == []

    def test_skips_overlay_without_file(self) -> None:
        import types  # noqa: PLC0415

        fake_mod = types.ModuleType("no_file")

        fake_overlay = MagicMock()
        type(fake_overlay).__module__ = "no_file"

        with (
            patch.dict(__import__("sys").modules, {"no_file": fake_mod}),
            patch("teatree.core.overlay_loader.get_all_overlays", return_value={"test": fake_overlay}),
        ):
            assert actions_views._find_overlay_repo_dirs() == []


# ---------------------------------------------------------------------------
# _get_t3_repo
# ---------------------------------------------------------------------------


class TestGetT3Repo:
    def test_returns_path_from_env_var(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"T3_REPO": str(tmp_path)}):
            result = actions_views._get_t3_repo()

        assert result == tmp_path

    def test_expands_user_in_env_var(self) -> None:
        with patch.dict(os.environ, {"T3_REPO": "~/my-teatree"}):
            result = actions_views._get_t3_repo()

        assert result == Path("~/my-teatree").expanduser()

    def test_auto_detects_from_package_location(self, tmp_path: Path) -> None:
        with (
            patch.dict(os.environ, {"T3_REPO": ""}, clear=False),
            patch("teatree.find_project_root", return_value=tmp_path),
        ):
            result = actions_views._get_t3_repo()

        assert result == tmp_path

    def test_returns_none_when_no_git_dir(self) -> None:
        with (
            patch.dict(os.environ, {"T3_REPO": ""}, clear=False),
            patch("teatree.find_project_root", return_value=None),
        ):
            result = actions_views._get_t3_repo()

        assert result is None


# ---------------------------------------------------------------------------
# SwitchBranchView
# ---------------------------------------------------------------------------


class TestSwitchBranchView(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    def test_get_returns_branches(self) -> None:
        branch_result = subprocess_mod.CompletedProcess([], 0, "main\ndev\nfeature-x\n", "")
        current_result = subprocess_mod.CompletedProcess([], 0, "dev\n", "")
        with (
            patch.object(actions_views, "_get_t3_repo", return_value=self.tmp_path),
            patch("teatree.utils.run.subprocess") as mock_subprocess,
        ):
            mock_subprocess.run.side_effect = [branch_result, current_result]
            mock_subprocess.TimeoutExpired = subprocess_mod.TimeoutExpired
            response = Client().get(reverse("teatree:dashboard-switch-branch"))

        assert response.status_code == 200
        data = response.json()
        assert data["branches"] == ["main", "dev", "feature-x"]
        assert data["current"] == "dev"

    def test_get_missing_repo_returns_400(self) -> None:
        with patch.object(actions_views, "_get_t3_repo", return_value=None):
            response = Client().get(reverse("teatree:dashboard-switch-branch"))

        assert response.status_code == 400

    def test_get_timeout_returns_500(self) -> None:
        with (
            patch.object(actions_views, "_get_t3_repo", return_value=self.tmp_path),
            patch("teatree.utils.run.subprocess") as mock_subprocess,
        ):
            mock_subprocess.TimeoutExpired = subprocess_mod.TimeoutExpired
            mock_subprocess.run.side_effect = subprocess_mod.TimeoutExpired(["git", "branch"], 10)
            response = Client().get(reverse("teatree:dashboard-switch-branch"))

        assert response.status_code == 500
        assert "timeout" in response.json()["error"]

    def test_post_switches_branch(self) -> None:
        completed = subprocess_mod.CompletedProcess([], 0, "Switched to branch 'main'\n", "")
        with (
            patch.object(actions_views, "_get_t3_repo", return_value=self.tmp_path),
            patch("teatree.utils.run.subprocess") as mock_subprocess,
        ):
            mock_subprocess.run.return_value = completed
            mock_subprocess.TimeoutExpired = subprocess_mod.TimeoutExpired
            response = Client().post(
                reverse("teatree:dashboard-switch-branch"),
                {"branch": "main"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["branch"] == "main"

    def test_post_empty_branch_returns_400(self) -> None:
        response = Client().post(reverse("teatree:dashboard-switch-branch"), {"branch": ""})

        assert response.status_code == 400
        assert "No branch" in response.json()["error"]

    def test_post_missing_repo_returns_400(self) -> None:
        with patch.object(actions_views, "_get_t3_repo", return_value=None):
            response = Client().post(
                reverse("teatree:dashboard-switch-branch"),
                {"branch": "main"},
            )

        assert response.status_code == 400

    def test_post_checkout_failure_returns_500(self) -> None:
        completed = subprocess_mod.CompletedProcess(
            [], 1, "", "error: pathspec 'nonexistent' did not match any file(s)\n"
        )
        with (
            patch.object(actions_views, "_get_t3_repo", return_value=self.tmp_path),
            patch("teatree.utils.run.subprocess") as mock_subprocess,
        ):
            mock_subprocess.run.return_value = completed
            mock_subprocess.TimeoutExpired = subprocess_mod.TimeoutExpired
            response = Client().post(
                reverse("teatree:dashboard-switch-branch"),
                {"branch": "nonexistent"},
            )

        assert response.status_code == 500
        assert "pathspec" in response.json()["error"]

    def test_post_timeout_returns_500(self) -> None:
        with (
            patch.object(actions_views, "_get_t3_repo", return_value=self.tmp_path),
            patch("teatree.utils.run.subprocess") as mock_subprocess,
        ):
            mock_subprocess.TimeoutExpired = subprocess_mod.TimeoutExpired
            mock_subprocess.run.side_effect = subprocess_mod.TimeoutExpired(["git", "checkout"], 15)
            response = Client().post(
                reverse("teatree:dashboard-switch-branch"),
                {"branch": "main"},
            )

        assert response.status_code == 500
        assert "timed out" in response.json()["error"]
