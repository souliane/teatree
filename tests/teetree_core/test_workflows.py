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

from teetree.core.models import Session, Task, Ticket, Worktree
from teetree.core.overlay import OverlayBase, ProvisionStep, RunCommands, ServiceSpec, ToolCommand
from teetree.core.overlay_loader import reset_overlay_cache

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
            {"name": "check-translations", "help": "Check translations", "management_command": "check_translations"},
        ]

    def get_workspace_repos(self) -> list[str]:
        return ["backend", "frontend"]


WORKFLOW_OVERLAY = "tests.teetree_core.test_workflows.WorkflowOverlay"

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
# Workflow 1: Full ticket lifecycle — create → provision → start → teardown
# ---------------------------------------------------------------------------


@override_settings(**WORKFLOW_SETTINGS)
def test_full_ticket_lifecycle_create_provision_start_teardown(tmp_path: Path) -> None:
    """Test the complete happy path: workspace ticket → lifecycle setup → start → teardown."""
    # Step 1: Create ticket with worktrees using real temp directories
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

    # Step 2: Provision both worktrees (lifecycle setup runs overlay provision steps)
    with patch("teetree.core.management.commands.lifecycle.subprocess") as mock_sp:
        mock_sp.run.return_value = MagicMock(returncode=0)
        backend_id = cast("int", call_command("lifecycle", "setup", str(wt_backend.id)))
        frontend_id = cast("int", call_command("lifecycle", "setup", str(wt_frontend.id)))

    # Verify provisioning ran
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

    # Verify .env.worktree was generated (last setup call wins — frontend's worktree)
    envfile = ticket_dir / ".env.worktree"
    assert envfile.is_file(), ".env.worktree should be generated during setup"
    env_content = envfile.read_text()
    assert "WT_VARIANT=testclient" in env_content
    assert "WT_DB_NAME=wt_42_testclient" in env_content
    assert "DJANGO_RUNSERVER_PORT=" in env_content
    assert "DJANGO_SETTINGS_MODULE=" in env_content  # overlay env vars appended
    # Symlinks from each repo worktree to the shared ticket dir file
    assert (ticket_dir / "backend" / ".env.worktree").is_symlink()
    assert (ticket_dir / "frontend" / ".env.worktree").is_symlink()

    # Step 3: Start services (lifecycle start transitions to services_up)
    call_command("lifecycle", "start", str(wt_backend.id))
    wt_backend.refresh_from_db()
    assert wt_backend.state == Worktree.State.SERVICES_UP

    # Step 4: Status check
    status = cast("dict[str, str]", call_command("lifecycle", "status", str(wt_backend.id)))
    assert status["state"] == Worktree.State.SERVICES_UP
    assert status["repo_path"] == "backend"
    assert status["branch"] == "ac-backend-42-ticket"

    # Step 5: Teardown — resets to created
    call_command("lifecycle", "teardown", str(wt_backend.id))
    wt_backend.refresh_from_db()
    assert wt_backend.state == Worktree.State.CREATED
    assert wt_backend.ports == {}
    assert wt_backend.db_name == ""


# ---------------------------------------------------------------------------
# Workflow 2: Port isolation — two worktrees get distinct ports
# ---------------------------------------------------------------------------


@override_settings(**WORKFLOW_SETTINGS)
def test_port_isolation_across_worktrees() -> None:
    """Verify two worktrees get distinct ports and DB names."""
    ticket1 = Ticket.objects.create(issue_url="https://example.com/issues/100", variant="alpha")
    ticket2 = Ticket.objects.create(issue_url="https://example.com/issues/200", variant="beta")

    wt1 = Worktree.objects.create(ticket=ticket1, repo_path="backend", branch="br-100")
    wt2 = Worktree.objects.create(ticket=ticket2, repo_path="backend", branch="br-200")

    with patch("teetree.core.management.commands.lifecycle.subprocess") as mock_sp:
        mock_sp.run.return_value = MagicMock(returncode=0)
        call_command("lifecycle", "setup", str(wt1.id))
        call_command("lifecycle", "setup", str(wt2.id))

    wt1.refresh_from_db()
    wt2.refresh_from_db()

    # Ports must differ
    assert wt1.ports["backend"] != wt2.ports["backend"]
    assert wt1.ports["frontend"] != wt2.ports["frontend"]
    # DB names based on ticket number + variant
    assert wt1.db_name == "wt_100_alpha"
    assert wt2.db_name == "wt_200_beta"


# ---------------------------------------------------------------------------
# Workflow 3: Task claim → work → complete → ticket state advance
# ---------------------------------------------------------------------------


@override_settings(**WORKFLOW_SETTINGS)
def test_task_lifecycle_claim_work_complete_advances_ticket() -> None:
    """Test the full task lifecycle: create → claim → complete → ticket advances."""
    # Set up ticket in the 'coded' state (ready for testing)
    ticket = Ticket.objects.create(issue_url="https://example.com/issues/99")
    ticket.scope(issue_url="https://example.com/issues/99", repos=["backend"])
    ticket.save()
    ticket.start()
    ticket.save()
    ticket.code()
    ticket.save()
    assert ticket.state == Ticket.State.CODED

    # Transition to tested (auto-schedules review task)
    ticket.test(passed=True)
    ticket.save()
    assert ticket.state == Ticket.State.TESTED

    # A review task should have been auto-scheduled
    review_task = Task.objects.filter(ticket=ticket, phase="reviewing").first()
    assert review_task is not None
    assert review_task.status == Task.Status.PENDING

    # Claim via management command
    claimed_id = cast(
        "int",
        call_command("tasks", "claim", execution_target="headless", claimed_by="review-agent"),
    )
    assert claimed_id == review_task.id

    review_task.refresh_from_db()
    assert review_task.status == Task.Status.CLAIMED
    assert review_task.claimed_by == "review-agent"

    # Complete the review task (should advance ticket to reviewed)
    review_task.complete_with_attempt(artifact_path="/tmp/review.md", exit_code=0)

    ticket.refresh_from_db()
    assert ticket.state == Ticket.State.REVIEWED

    # A shipping task should now be auto-scheduled
    ship_task = Task.objects.filter(ticket=ticket, phase="shipping").first()
    assert ship_task is not None


# ---------------------------------------------------------------------------
# Workflow 4: Dashboard interaction — create task → cancel → rework
# ---------------------------------------------------------------------------


@override_settings(**WORKFLOW_SETTINGS)
def test_dashboard_create_task_and_cancel() -> None:
    """Test the dashboard view workflow: create headless task → cancel it."""
    client = Client()

    ticket = Ticket.objects.create(issue_url="https://example.com/issues/77")
    Session.objects.create(ticket=ticket, agent_id="dashboard")

    # Create task via dashboard view
    with patch("teetree.core.tasks.execute_headless_task") as mock_enqueue:
        mock_enqueue.enqueue = MagicMock()
        resp = client.post(
            f"/tickets/{ticket.pk}/create-task/",
            {"phase": "coding", "target": "headless"},
        )
    assert resp.status_code == 200
    data = resp.json()
    task_id = data["task_id"]
    assert data["status"] == Task.Status.CLAIMED

    # Cancel the task
    resp = client.post(f"/tasks/{task_id}/cancel/")
    assert resp.status_code == 200
    cancel_data = resp.json()
    assert cancel_data["status"] == Task.Status.FAILED

    # Verify task is failed in DB
    task = Task.objects.get(pk=task_id)
    assert task.status == Task.Status.FAILED


# ---------------------------------------------------------------------------
# Workflow 5: Ticket state machine — full progression with transitions
# ---------------------------------------------------------------------------


@override_settings(**WORKFLOW_SETTINGS)
def test_ticket_state_progression_via_views() -> None:
    """Test ticket state progression through the TicketTransitionView."""
    client = Client()

    ticket = Ticket.objects.create(issue_url="https://example.com/issues/10")

    def transition(name: str, expected_status: int = 200) -> dict:
        resp = client.post(f"/tickets/{ticket.pk}/transition/", {"transition": name})
        assert resp.status_code == expected_status, f"Transition {name} returned {resp.status_code}: {resp.content}"
        return resp.json()

    # Scope → Start → Code
    result = transition("scope")
    assert result["state"] == "Scoped"

    result = transition("start")
    assert result["state"] == "Started"

    result = transition("code")
    assert result["state"] == "Coded"

    # Invalid transition (can't go from coded to shipped directly)
    result = transition("ship", expected_status=409)
    assert "not allowed" in result["error"]

    # Unknown transition
    result = transition("nonexistent", expected_status=400)
    assert "Unknown transition" in result["error"]


# ---------------------------------------------------------------------------
# Workflow 6: Provision → run backend → env passthrough
# ---------------------------------------------------------------------------


@override_settings(**WORKFLOW_SETTINGS)
def test_run_backend_uses_overlay_env_and_starts_services() -> None:
    """Test that run backend starts Docker services and passes overlay env to subprocess."""
    ticket = Ticket.objects.create(issue_url="https://example.com/issues/50")
    wt = Worktree.objects.create(ticket=ticket, repo_path="backend", branch="feature")

    # Provision first
    with patch("teetree.core.management.commands.lifecycle.subprocess"):
        call_command("lifecycle", "setup", str(wt.id))

    wt.refresh_from_db()

    # Now run backend — should start services and then run the backend command
    with patch("teetree.core.management.commands.run.subprocess") as mock_sp:
        mock_sp.run.return_value = MagicMock(returncode=0)
        result = cast("str", call_command("run", "backend", str(wt.id)))

    assert result == "Backend started."

    # Verify subprocess.run was called for:
    # 1-2. Docker service starts (postgres, redis)
    # 3. The actual backend command
    calls = mock_sp.run.call_args_list
    assert len(calls) == 3

    # First two are service starts
    assert "docker compose" in calls[0].args[0]
    assert "docker compose" in calls[1].args[0]

    # Last one is the backend command with overlay env
    backend_call = calls[2]
    assert "runserver" in backend_call.args[0]
    env = backend_call.kwargs.get("env", {})
    assert env.get("DJANGO_SETTINGS_MODULE") == "project.settings"
    assert env.get("POSTGRES_DB") == wt.db_name
    assert "VIRTUAL_ENV" not in env


# ---------------------------------------------------------------------------
# Workflow 7: Password reset runs automatically during provisioning
# ---------------------------------------------------------------------------


@override_settings(**WORKFLOW_SETTINGS)
def test_provision_runs_password_reset_automatically() -> None:
    """Verify lifecycle setup calls get_reset_passwords_command and runs it."""
    ticket = Ticket.objects.create(issue_url="https://example.com/issues/60")
    wt = Worktree.objects.create(ticket=ticket, repo_path="backend", branch="feature")

    with patch("teetree.core.management.commands.lifecycle.subprocess") as mock_sp:
        mock_sp.run.return_value = MagicMock(returncode=0)
        call_command("lifecycle", "setup", str(wt.id))

    # The last subprocess.run call should be the password reset
    calls = mock_sp.run.call_args_list
    reset_call = calls[-1]
    assert "reset_passwords" in reset_call.args[0]


# ---------------------------------------------------------------------------
# Workflow 8: Tool command dispatching
# ---------------------------------------------------------------------------


@override_settings(**WORKFLOW_SETTINGS)
def test_tool_list_and_run_dispatches_overlay_commands() -> None:
    """Test the tool management command lists and runs overlay tools."""
    # List tools
    result = cast("str", call_command("tool", "list"))
    assert "check-translations" in result
    assert "Check translations" in result

    # Run a tool
    with patch("teetree.core.management.commands.tool.subprocess") as mock_sp:
        mock_sp.run.return_value = MagicMock(returncode=0)
        result = cast("str", call_command("tool", "run", "check-translations"))

    assert result == "Tool 'check-translations' completed."
    mock_sp.run.assert_called_once()
    assert "check_translations" in mock_sp.run.call_args.args[0]


# ---------------------------------------------------------------------------
# Workflow 9: Rework cycle — ticket goes back to started, pending tasks cancelled
# ---------------------------------------------------------------------------


@override_settings(**WORKFLOW_SETTINGS)
def test_rework_cancels_pending_tasks_and_resets_ticket() -> None:
    """Test the rework flow: ticket is sent back, pending tasks are cancelled."""
    ticket = Ticket.objects.create(issue_url="https://example.com/issues/88")
    ticket.scope(repos=["backend"])
    ticket.save()
    ticket.start()
    ticket.save()
    ticket.code()
    ticket.save()

    # Create some tasks
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

    # Rework
    ticket.rework()
    ticket.save()

    assert ticket.state == Ticket.State.STARTED

    pending_task.refresh_from_db()
    claimed_task.refresh_from_db()
    completed_task.refresh_from_db()

    assert pending_task.status == Task.Status.FAILED
    assert claimed_task.status == Task.Status.FAILED
    assert completed_task.status == Task.Status.COMPLETED  # Already done, not affected


# ---------------------------------------------------------------------------
# Workflow 10: Needs user input → interactive followup scheduling
# ---------------------------------------------------------------------------


@override_settings(**WORKFLOW_SETTINGS)
def test_headless_task_needing_user_input_schedules_interactive_followup() -> None:
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

    # Complete with needs_user_input result
    task.complete_with_attempt(
        exit_code=0,
        result={"needs_user_input": True, "user_input_reason": "Need approval for DB migration"},
    )

    # The original task should be completed
    task.refresh_from_db()
    assert task.status == Task.Status.COMPLETED

    # An interactive followup task should exist
    followup = Task.objects.filter(
        ticket=ticket,
        execution_target=Task.ExecutionTarget.INTERACTIVE,
        parent_task=task,
    ).first()
    assert followup is not None
    assert followup.status == Task.Status.PENDING
    assert "approval" in followup.execution_reason.lower()


# ---------------------------------------------------------------------------
# Workflow 11: Clean command — only removes CREATED worktrees
# ---------------------------------------------------------------------------


@override_settings(**WORKFLOW_SETTINGS)
def test_clean_command_only_removes_created_worktrees() -> None:
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
# Workflow 12: DB refresh resets worktree to provisioned
# ---------------------------------------------------------------------------


@override_settings(**WORKFLOW_SETTINGS)
def test_db_refresh_resets_services_up_to_provisioned() -> None:
    """Verify db_refresh transition takes worktree from services_up back to provisioned."""
    ticket = Ticket.objects.create(issue_url="https://example.com/issues/33")
    wt = Worktree.objects.create(ticket=ticket, repo_path="backend", branch="feature")

    # Progress to services_up
    wt.provision()
    wt.save()
    wt.start_services(services=["backend"])
    wt.save()
    assert wt.state == Worktree.State.SERVICES_UP

    # Refresh DB
    wt.db_refresh()
    wt.save()
    assert wt.state == Worktree.State.PROVISIONED
    assert "db_refreshed_at" in (wt.extra or {})


# ---------------------------------------------------------------------------
# Workflow 13: Real workspace ticket → lifecycle setup → run backend
# Uses real temp directories, mocks only git and subprocess calls.
# Tests the actual management command output and DB state transitions.
# ---------------------------------------------------------------------------


@override_settings(**WORKFLOW_SETTINGS)
def test_workspace_ticket_through_lifecycle_setup_to_run_backend(tmp_path: Path) -> None:  # noqa: PLR0915
    """End-to-end workflow: workspace ticket → lifecycle setup → run backend.

    Uses a real temp directory for the workspace. Mocks git worktree add
    to create real directories. Verifies command output, DB state, and env
    passthrough at each step.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Create fake repos (with .git directories so they're recognized)
    for repo in ("backend", "frontend"):
        repo_dir = workspace / repo
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        (repo_dir / ".python-version").write_text("3.12.6")

    def fake_subprocess_run(cmd, **kwargs):
        """Simulate git worktree add by creating the directory."""
        result = MagicMock(returncode=0, stdout="", stderr="")
        if isinstance(cmd, list) and "worktree" in cmd and "add" in cmd:
            # git worktree add -b <branch> <path>
            wt_path = Path(cmd[-1])
            wt_path.mkdir(parents=True, exist_ok=True)
            (wt_path / ".git").write_text("gitdir: /fake/worktree")
        return result

    # --- Step 1: workspace ticket ---
    with (
        patch.dict("os.environ", {"T3_WORKSPACE_DIR": str(workspace), "T3_BRANCH_PREFIX": "ac"}),
        patch(
            "teetree.core.management.commands.workspace.subprocess.run",
            side_effect=fake_subprocess_run,
        ),
        patch("teetree.core.management.commands.workspace._workspace_dir", return_value=workspace),
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

    # Verify ticket was created with correct state
    ticket = Ticket.objects.get(pk=ticket_id)
    assert ticket.state == Ticket.State.SCOPED
    assert ticket.variant == "testclient"
    assert ticket.repos == ["backend", "frontend"]
    assert ticket.issue_url == "https://gitlab.com/org/repo/-/issues/999"

    # Verify worktrees were created in DB
    worktrees = list(Worktree.objects.filter(ticket=ticket).order_by("repo_path"))
    assert len(worktrees) == 2
    assert worktrees[0].repo_path == "backend"
    assert worktrees[1].repo_path == "frontend"
    assert worktrees[0].branch == "ac-backend-999-ticket"

    # Verify worktree paths exist on disk
    for wt in worktrees:
        stored_path = (wt.extra or {}).get("worktree_path")
        assert stored_path is not None
        assert Path(stored_path).is_dir()

    # --- Step 2: lifecycle setup (provision) ---
    backend_wt = worktrees[0]
    assert backend_wt.state == Worktree.State.CREATED

    with patch("teetree.core.management.commands.lifecycle.subprocess") as mock_lc_sp:
        mock_lc_sp.run.return_value = MagicMock(returncode=0)
        setup_result = cast("int", call_command("lifecycle", "setup", str(backend_wt.id)))

    assert setup_result == backend_wt.id

    backend_wt.refresh_from_db()
    assert backend_wt.state == Worktree.State.PROVISIONED
    assert backend_wt.ports["backend"] >= 8001
    assert backend_wt.ports["frontend"] >= 4201
    assert backend_wt.db_name == "wt_999_testclient"

    # Password reset was called
    reset_calls = [c for c in mock_lc_sp.run.call_args_list if "reset_passwords" in str(c)]
    assert len(reset_calls) == 1

    # --- Step 3: run backend ---
    with patch("teetree.core.management.commands.run.subprocess") as mock_run_sp:
        mock_run_sp.run.return_value = MagicMock(returncode=0)
        run_result = cast("str", call_command("run", "backend", str(backend_wt.id)))

    assert run_result == "Backend started."

    # Verify the actual backend command was called with correct env
    backend_calls = [c for c in mock_run_sp.run.call_args_list if "runserver" in str(c)]
    assert len(backend_calls) == 1

    env = backend_calls[0].kwargs.get("env", {})
    assert env["DJANGO_SETTINGS_MODULE"] == "project.settings"
    assert env["POSTGRES_DB"] == "wt_999_testclient"
    assert env["DJANGO_RUNSERVER_PORT"] == str(backend_wt.ports["backend"])
    assert "VIRTUAL_ENV" not in env

    # Verify docker services were started before the backend
    service_calls = [c for c in mock_run_sp.run.call_args_list if "docker compose" in str(c)]
    assert len(service_calls) == 2  # postgres + redis
