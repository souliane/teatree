"""Integration tests covering full workflows through the Django management commands and views.

These are NOT end-to-end tests (no real servers/git). They exercise real code paths
through the Django ORM, management commands, and test client, mocking only external
dependencies (subprocess, filesystem, HTTP).
"""

from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from teatree.core.models import Session, Task, Ticket, Worktree
from teatree.core.overlay import OverlayBase, ProvisionStep, RunCommands, ServiceSpec, ToolCommand
from teatree.core.overlay_loader import reset_overlay_cache

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.filterwarnings(
        "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
    ),
]


class WorkflowOverlay(OverlayBase):
    """Rich overlay that supports the full lifecycle for workflow tests."""

    def get_repos(self) -> list[str]:
        return ["backend", "frontend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        def _record_provision() -> None:
            extra = cast("dict[str, object]", worktree.extra or {})
            extra["provisioned_by_overlay"] = True
            worktree.extra = extra
            worktree.save(update_fields=["extra"])

        return [
            ProvisionStep(name="symlinks", callable=lambda: None, description="Link .venv"),
            ProvisionStep(name="migrations", callable=_record_provision, description="Run migrations"),
        ]

    def get_run_commands(self, worktree: Worktree) -> RunCommands:
        return {
            "backend": f"python manage.py runserver {worktree.ports.get('backend', 8000)}",
            "frontend": f"npm run start --port {worktree.ports.get('frontend', 4200)}",
            "build-frontend": "npm run build",
        }

    def get_env_extra(self, worktree: Worktree) -> dict[str, str]:
        ports = worktree.ports or {}
        return {
            "DJANGO_SETTINGS_MODULE": "project.settings",
            "POSTGRES_DB": worktree.db_name or "test_db",
            "DJANGO_RUNSERVER_PORT": str(ports.get("backend", 8000)),
        }

    def get_services_config(self, worktree: Worktree) -> dict[str, ServiceSpec]:
        return {
            "postgres": {
                "shared": True,
                "start_command": "docker compose up -d db",
            },
            "redis": {
                "shared": True,
                "start_command": "docker compose up -d redis",
            },
        }

    def get_test_command(self, worktree: Worktree) -> str:
        return f"cd {worktree.repo_path} && pytest"

    def get_reset_passwords_command(self, worktree: Worktree) -> str:
        return f"cd {worktree.repo_path} && python manage.py reset_passwords"

    def get_tool_commands(self) -> list[ToolCommand]:
        return [
            {"name": "check-translations", "help": "Check translations", "command": "check_translations"},
        ]

    def get_workspace_repos(self) -> list[str]:
        return ["backend", "frontend"]


WORKFLOW_OVERLAY = "tests.teatree_core.test_workflows.WorkflowOverlay"

WORKFLOW_SETTINGS = {
    "TEATREE_OVERLAY_CLASS": WORKFLOW_OVERLAY,
    "TEATREE_HEADLESS_RUNTIME": "claude-code",
    "TEATREE_INTERACTIVE_RUNTIME": "codex",
    "TEATREE_TERMINAL_MODE": "same-terminal",
}


@pytest.fixture(autouse=True)
def _clear_overlay() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()


# ---------------------------------------------------------------------------
# Lifecycle provisioning workflows
# ---------------------------------------------------------------------------


class TestLifecycleProvision:
    @override_settings(**WORKFLOW_SETTINGS)
    def test_full_create_provision_start_teardown(self, tmp_path: Path) -> None:
        """Test the complete happy path: workspace ticket -> lifecycle setup -> start -> teardown."""
        ticket_dir = tmp_path / "ac-backend-42-ticket"
        ticket_dir.mkdir()
        (ticket_dir / "backend").mkdir()
        (ticket_dir / "frontend").mkdir()

        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/42",
            variant="testclient",
        )
        ticket.scope(
            issue_url="https://gitlab.com/org/repo/-/issues/42",
            variant="testclient",
            repos=["backend", "frontend"],
        )
        ticket.save()

        wt_backend = Worktree.objects.create(
            ticket=ticket,
            repo_path="backend",
            branch="ac-backend-42-ticket",
            extra={"worktree_path": str(ticket_dir / "backend")},
        )
        wt_frontend = Worktree.objects.create(
            ticket=ticket,
            repo_path="frontend",
            branch="ac-backend-42-ticket",
            extra={"worktree_path": str(ticket_dir / "frontend")},
        )

        backend_path = str(ticket_dir / "backend")
        frontend_path = str(ticket_dir / "frontend")
        with patch("teatree.core.management.commands.lifecycle.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(returncode=0)
            backend_id = cast("int", call_command("lifecycle", "setup", path=backend_path))
            frontend_id = cast("int", call_command("lifecycle", "setup", path=frontend_path))

        assert backend_id == wt_backend.id
        assert frontend_id == wt_frontend.id

        wt_backend.refresh_from_db()
        wt_frontend.refresh_from_db()
        assert wt_backend.state == Worktree.State.PROVISIONED
        assert wt_frontend.state == Worktree.State.PROVISIONED
        assert wt_backend.ports["backend"] != wt_frontend.ports["backend"]
        assert wt_backend.db_name == "wt_42_testclient"
        assert wt_backend.extra.get("provisioned_by_overlay") is True
        assert wt_frontend.extra.get("provisioned_by_overlay") is True

        envfile = ticket_dir / ".env.worktree"
        assert envfile.is_file(), ".env.worktree should be generated during setup"
        env_content = envfile.read_text()
        assert "WT_VARIANT=testclient" in env_content
        assert "WT_DB_NAME=wt_42_testclient" in env_content
        assert "DJANGO_RUNSERVER_PORT=" in env_content
        assert "DJANGO_SETTINGS_MODULE=" in env_content
        assert (ticket_dir / "backend" / ".env.worktree").is_symlink()
        assert (ticket_dir / "frontend" / ".env.worktree").is_symlink()

        call_command("lifecycle", "start", path=backend_path)
        wt_backend.refresh_from_db()
        assert wt_backend.state == Worktree.State.SERVICES_UP

        status = cast("dict[str, str]", call_command("lifecycle", "status", path=backend_path))
        assert status["state"] == Worktree.State.SERVICES_UP
        assert status["repo_path"] == "backend"
        assert status["branch"] == "ac-backend-42-ticket"

        call_command("lifecycle", "teardown", path=backend_path)
        wt_backend.refresh_from_db()
        assert wt_backend.state == Worktree.State.CREATED
        assert wt_backend.ports == {}
        assert wt_backend.db_name == ""

    @override_settings(**WORKFLOW_SETTINGS)
    def test_port_isolation_across_worktrees(self, tmp_path: Path) -> None:
        """Verify two worktrees get distinct ports and DB names."""
        wt1_dir = tmp_path / "wt1"
        wt2_dir = tmp_path / "wt2"
        wt1_dir.mkdir()
        wt2_dir.mkdir()

        ticket1 = Ticket.objects.create(issue_url="https://example.com/issues/100", variant="alpha")
        ticket2 = Ticket.objects.create(issue_url="https://example.com/issues/200", variant="beta")

        wt1 = Worktree.objects.create(
            ticket=ticket1, repo_path="backend", branch="br-100", extra={"worktree_path": str(wt1_dir)}
        )
        wt2 = Worktree.objects.create(
            ticket=ticket2, repo_path="backend", branch="br-200", extra={"worktree_path": str(wt2_dir)}
        )

        with patch("teatree.core.management.commands.lifecycle.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(returncode=0)
            call_command("lifecycle", "setup", path=str(wt1_dir))
            call_command("lifecycle", "setup", path=str(wt2_dir))

        wt1.refresh_from_db()
        wt2.refresh_from_db()

        assert wt1.ports["backend"] != wt2.ports["backend"]
        assert wt1.ports["frontend"] != wt2.ports["frontend"]
        assert wt1.db_name == "wt_100_alpha"
        assert wt2.db_name == "wt_200_beta"

    @override_settings(**WORKFLOW_SETTINGS)
    def test_password_reset_runs_automatically(self, tmp_path: Path) -> None:
        """Verify lifecycle setup calls get_reset_passwords_command and runs it."""
        wt_dir = tmp_path / "backend"
        wt_dir.mkdir()

        ticket = Ticket.objects.create(issue_url="https://example.com/issues/60")
        Worktree.objects.create(
            ticket=ticket, repo_path="backend", branch="feature", extra={"worktree_path": str(wt_dir)}
        )

        with patch("teatree.core.management.commands.lifecycle.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(returncode=0)
            call_command("lifecycle", "setup", path=str(wt_dir))

        calls = mock_sp.run.call_args_list
        reset_call = calls[-1]
        assert "reset_passwords" in reset_call.args[0]


# ---------------------------------------------------------------------------
# Task lifecycle workflows
# ---------------------------------------------------------------------------


class TestTaskWorkflow:
    @override_settings(**WORKFLOW_SETTINGS)
    def test_claim_work_complete_advances_ticket(self) -> None:
        """Test the full task lifecycle: create -> claim -> complete -> ticket advances."""
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/99")
        ticket.scope(issue_url="https://example.com/issues/99", repos=["backend"])
        ticket.save()
        ticket.start()
        ticket.save()
        ticket.code()
        ticket.save()
        assert ticket.state == Ticket.State.CODED

        ticket.test(passed=True)
        ticket.save()
        assert ticket.state == Ticket.State.TESTED

        review_task = Task.objects.filter(ticket=ticket, phase="reviewing").first()
        assert review_task is not None
        assert review_task.status == Task.Status.PENDING

        claimed_id = cast(
            "int",
            call_command("tasks", "claim", execution_target="headless", claimed_by="review-agent"),
        )
        assert claimed_id == review_task.id

        review_task.refresh_from_db()
        assert review_task.status == Task.Status.CLAIMED
        assert review_task.claimed_by == "review-agent"

        review_task.complete_with_attempt(artifact_path="/tmp/review.md", exit_code=0)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED

        ship_task = Task.objects.filter(ticket=ticket, phase="shipping").first()
        assert ship_task is not None

    @override_settings(**WORKFLOW_SETTINGS)
    def test_rework_cancels_pending_tasks_and_resets_ticket(self) -> None:
        """Test the rework flow: ticket is sent back, pending tasks are cancelled."""
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/88")
        ticket.scope(repos=["backend"])
        ticket.save()
        ticket.start()
        ticket.save()
        ticket.code()
        ticket.save()

        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        pending_task = Task.objects.create(ticket=ticket, session=session, status=Task.Status.PENDING)
        claimed_task = Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.CLAIMED,
            claimed_by="worker",
        )
        completed_task = Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.COMPLETED,
        )

        ticket.rework()
        ticket.save()

        assert ticket.state == Ticket.State.STARTED

        pending_task.refresh_from_db()
        claimed_task.refresh_from_db()
        completed_task.refresh_from_db()

        assert pending_task.status == Task.Status.FAILED
        assert claimed_task.status == Task.Status.FAILED
        assert completed_task.status == Task.Status.COMPLETED  # Already done, not affected

    @override_settings(**WORKFLOW_SETTINGS)
    def test_headless_needing_user_input_schedules_interactive_followup(self) -> None:
        """When a headless task reports needs_user_input, an interactive followup is created."""
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/71")
        session = Session.objects.create(ticket=ticket, agent_id="headless-agent")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            execution_target=Task.ExecutionTarget.HEADLESS,
        )
        task.claim(claimed_by="worker-1")

        task.complete_with_attempt(
            exit_code=0,
            result={"needs_user_input": True, "user_input_reason": "Need approval for DB migration"},
        )

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

        followup = Task.objects.filter(
            ticket=ticket,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            parent_task=task,
        ).first()
        assert followup is not None
        assert followup.status == Task.Status.PENDING
        assert "approval" in followup.execution_reason.lower()


# ---------------------------------------------------------------------------
# Dashboard and views workflows
# ---------------------------------------------------------------------------


class TestDashboardAndViews:
    @override_settings(**WORKFLOW_SETTINGS)
    def test_create_task_and_cancel(self) -> None:
        """Test the dashboard view workflow: create headless task -> cancel it."""
        client = Client()

        ticket = Ticket.objects.create(issue_url="https://example.com/issues/77")
        Session.objects.create(ticket=ticket, agent_id="dashboard")

        with patch("teatree.core.tasks.execute_headless_task") as mock_enqueue:
            mock_enqueue.enqueue = MagicMock()
            resp = client.post(
                f"/tickets/{ticket.pk}/create-task/",
                {"phase": "coding", "target": "headless"},
            )
        assert resp.status_code == 200
        data = resp.json()
        task_id = data["task_id"]
        assert data["status"] == Task.Status.CLAIMED

        resp = client.post(f"/tasks/{task_id}/cancel/")
        assert resp.status_code == 200
        cancel_data = resp.json()
        assert cancel_data["status"] == Task.Status.FAILED

        task = Task.objects.get(pk=task_id)
        assert task.status == Task.Status.FAILED

    @override_settings(**WORKFLOW_SETTINGS)
    def test_ticket_state_progression_via_views(self) -> None:
        """Test ticket state progression through the TicketTransitionView."""
        client = Client()

        ticket = Ticket.objects.create(issue_url="https://example.com/issues/10")

        def transition(name: str, expected_status: int = 200) -> dict:
            resp = client.post(f"/tickets/{ticket.pk}/transition/", {"transition": name})
            assert resp.status_code == expected_status, f"Transition {name} returned {resp.status_code}: {resp.content}"
            return resp.json()

        result = transition("scope")
        assert result["state"] == "Scoped"

        result = transition("start")
        assert result["state"] == "Started"

        result = transition("code")
        assert result["state"] == "Coded"

        result = transition("ship", expected_status=409)
        assert "not allowed" in result["error"]

        result = transition("nonexistent", expected_status=400)
        assert "Unknown transition" in result["error"]


# ---------------------------------------------------------------------------
# Run backend workflows
# ---------------------------------------------------------------------------


class TestRunBackend:
    @override_settings(**WORKFLOW_SETTINGS)
    def test_uses_overlay_env_and_starts_services(self, tmp_path: Path) -> None:
        """Test that run backend starts Docker services and passes overlay env to subprocess."""
        wt_dir = tmp_path / "backend"
        wt_dir.mkdir()

        ticket = Ticket.objects.create(issue_url="https://example.com/issues/50")
        wt = Worktree.objects.create(
            ticket=ticket, repo_path="backend", branch="feature", extra={"worktree_path": str(wt_dir)}
        )

        with patch("teatree.core.management.commands.lifecycle.subprocess"):
            call_command("lifecycle", "setup", path=str(wt_dir))

        wt.refresh_from_db()

        with patch("teatree.core.management.commands.run.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(returncode=0)
            result = cast("str", call_command("run", "backend", path=str(wt_dir)))

        assert result == "Backend started."

        calls = mock_sp.run.call_args_list
        assert len(calls) == 3

        assert "docker compose" in calls[0].args[0]
        assert "docker compose" in calls[1].args[0]

        backend_call = calls[2]
        assert "runserver" in backend_call.args[0]
        env = backend_call.kwargs.get("env", {})
        assert env.get("DJANGO_SETTINGS_MODULE") == "project.settings"
        assert env.get("POSTGRES_DB") == wt.db_name
        assert "VIRTUAL_ENV" not in env

    @override_settings(**WORKFLOW_SETTINGS)
    def test_workspace_ticket_through_lifecycle_to_run(self, tmp_path: Path) -> None:  # noqa: PLR0915
        """End-to-end workflow: workspace ticket -> lifecycle setup -> run backend.

        Uses a real temp directory for the workspace. Mocks git worktree add
        to create real directories. Verifies command output, DB state, and env
        passthrough at each step.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        for repo in ("backend", "frontend"):
            repo_dir = workspace / repo
            repo_dir.mkdir()
            (repo_dir / ".git").mkdir()
            (repo_dir / ".python-version").write_text("3.12.6")

        def fake_subprocess_run(cmd, **kwargs):
            """Simulate git worktree add by creating the directory."""
            result = MagicMock(returncode=0, stdout="", stderr="")
            if isinstance(cmd, list) and "worktree" in cmd and "add" in cmd:
                wt_path = Path(cmd[-1])
                wt_path.mkdir(parents=True, exist_ok=True)
                (wt_path / ".git").write_text("gitdir: /fake/worktree")
            return result

        # --- Step 1: workspace ticket ---
        with (
            patch.dict("os.environ", {"T3_WORKSPACE_DIR": str(workspace), "T3_BRANCH_PREFIX": "ac"}),
            patch(
                "teatree.core.management.commands.workspace.subprocess.run",
                side_effect=fake_subprocess_run,
            ),
            patch("teatree.core.management.commands.workspace._workspace_dir", return_value=workspace),
        ):
            ticket_id = cast(
                "int",
                call_command(
                    "workspace",
                    "ticket",
                    "https://gitlab.com/org/repo/-/issues/999",
                    "--variant",
                    "testclient",
                ),
            )

        ticket = Ticket.objects.get(pk=ticket_id)
        assert ticket.state == Ticket.State.SCOPED
        assert ticket.variant == "testclient"
        assert ticket.repos == ["backend", "frontend"]
        assert ticket.issue_url == "https://gitlab.com/org/repo/-/issues/999"

        worktrees = list(Worktree.objects.filter(ticket=ticket).order_by("repo_path"))
        assert len(worktrees) == 2
        assert worktrees[0].repo_path == "backend"
        assert worktrees[1].repo_path == "frontend"
        assert worktrees[0].branch == "ac-backend-999-ticket"

        for wt in worktrees:
            stored_path = (wt.extra or {}).get("worktree_path")
            assert stored_path is not None
            assert Path(stored_path).is_dir()

        # --- Step 2: lifecycle setup (provision) ---
        backend_wt = worktrees[0]
        backend_wt_path = (backend_wt.extra or {}).get("worktree_path", "")
        assert backend_wt.state == Worktree.State.CREATED

        with patch("teatree.core.management.commands.lifecycle.subprocess") as mock_lc_sp:
            mock_lc_sp.run.return_value = MagicMock(returncode=0)
            setup_result = cast("int", call_command("lifecycle", "setup", path=backend_wt_path))

        assert setup_result == backend_wt.id

        backend_wt.refresh_from_db()
        assert backend_wt.state == Worktree.State.PROVISIONED
        assert backend_wt.ports["backend"] >= 8001
        assert backend_wt.ports["frontend"] >= 4201
        assert backend_wt.db_name == "wt_999_testclient"

        # Setup now provisions ALL ticket worktrees (backend + frontend)
        reset_calls = [c for c in mock_lc_sp.run.call_args_list if "reset_passwords" in str(c)]
        assert len(reset_calls) == 2

        # --- Step 3: run backend ---
        with patch("teatree.core.management.commands.run.subprocess") as mock_run_sp:
            mock_run_sp.run.return_value = MagicMock(returncode=0)
            run_result = cast("str", call_command("run", "backend", path=backend_wt_path))

        assert run_result == "Backend started."

        backend_calls = [c for c in mock_run_sp.run.call_args_list if "runserver" in str(c)]
        assert len(backend_calls) == 1

        env = backend_calls[0].kwargs.get("env", {})
        assert env["DJANGO_SETTINGS_MODULE"] == "project.settings"
        assert env["POSTGRES_DB"] == "wt_999_testclient"
        assert env["DJANGO_RUNSERVER_PORT"] == str(backend_wt.ports["backend"])
        assert "VIRTUAL_ENV" not in env

        service_calls = [c for c in mock_run_sp.run.call_args_list if "docker compose" in str(c)]
        assert len(service_calls) == 2  # postgres + redis


# ---------------------------------------------------------------------------
# Tool and clean commands
# ---------------------------------------------------------------------------


class TestToolAndCleanCommands:
    @override_settings(**WORKFLOW_SETTINGS)
    def test_tool_list_and_run_dispatches_overlay_commands(self) -> None:
        """Test the tool management command lists and runs overlay tools."""
        result = cast("str", call_command("tool", "list"))
        assert "check-translations" in result
        assert "Check translations" in result

        with patch("teatree.core.management.commands.tool.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(returncode=0)
            result = cast("str", call_command("tool", "run", "check-translations"))

        assert result == "Tool 'check-translations' completed."
        mock_sp.run.assert_called_once()
        assert "check_translations" in mock_sp.run.call_args.args[0]

    @override_settings(**WORKFLOW_SETTINGS)
    def test_clean_only_removes_created_worktrees(self) -> None:
        """Verify clean-all only removes worktrees in CREATED state."""
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/30")

        created_wt = Worktree.objects.create(ticket=ticket, repo_path="stale", branch="old")
        active_wt = Worktree.objects.create(ticket=ticket, repo_path="active", branch="current")
        active_wt.provision()
        active_wt.save()

        result = cast("list[str]", call_command("workspace", "clean-all"))

        assert len(result) == 1
        assert "stale" in result[0]
        assert Worktree.objects.filter(pk=created_wt.pk).count() == 0
        assert Worktree.objects.filter(pk=active_wt.pk).count() == 1


# ---------------------------------------------------------------------------
# DB refresh
# ---------------------------------------------------------------------------


class TestDbRefresh:
    @override_settings(**WORKFLOW_SETTINGS)
    def test_resets_services_up_to_provisioned(self) -> None:
        """Verify db_refresh transition takes worktree from services_up back to provisioned."""
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/33")
        wt = Worktree.objects.create(ticket=ticket, repo_path="backend", branch="feature")

        wt.provision()
        wt.save()
        wt.start_services(services=["backend"])
        wt.save()
        assert wt.state == Worktree.State.SERVICES_UP

        wt.db_refresh()
        wt.save()
        assert wt.state == Worktree.State.PROVISIONED
        assert "db_refreshed_at" in (wt.extra or {})
