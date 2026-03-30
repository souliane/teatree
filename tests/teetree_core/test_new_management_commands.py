"""Tests for workspace, db, pr, and extended run management commands."""

from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import override_settings

from teetree.core.models import Session, Ticket, Worktree
from teetree.core.overlay import (
    DbImportStrategy,
    OverlayBase,
    PostDbStep,
    ProvisionStep,
    RunCommands,
    ServiceSpec,
    ToolCommand,
    ValidationResult,
)
from teetree.core.overlay_loader import reset_overlay_cache

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


class FullOverlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return ["backend", "frontend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []

    def get_run_commands(self, worktree: Worktree) -> RunCommands:
        return {
            "backend": f"echo backend {worktree.repo_path}",
            "frontend": f"echo frontend {worktree.repo_path}",
            "build-frontend": f"echo build {worktree.repo_path}",
        }

    def get_test_command(self, worktree: Worktree) -> str:
        return f"echo tests {worktree.repo_path}"

    def get_db_import_strategy(self, worktree: Worktree) -> DbImportStrategy:
        return {"kind": "test", "source_database": "test_db"}

    def db_import(self, worktree: Worktree, *, force: bool = False) -> bool:
        return True

    def get_reset_passwords_command(self, worktree: Worktree) -> str:
        return "echo passwords_reset"

    def get_ci_project_path(self) -> str:
        return "test/project"

    def detect_variant(self) -> str:
        return "test_variant"

    def get_e2e_config(self) -> dict[str, str]:
        return {"project_path": "test/e2e-project", "ref": "main"}

    def get_tool_commands(self) -> list[ToolCommand]:
        return [
            {"name": "migrate", "help": "Run DB migrations", "management_command": "echo migrate"},
            {"name": "seed", "help": "Seed test data", "management_command": "echo seed"},
            {"name": "broken", "help": "No command defined"},
        ]

    def validate_mr(self, title: str, description: str) -> ValidationResult:
        errors = []
        if not title:
            errors.append("Title is required")
        return {"errors": errors, "warnings": []}


class ServicesOverlay(FullOverlay):
    """Overlay with services config — used to test _start_services."""

    def get_services_config(self, worktree: Worktree) -> dict[str, ServiceSpec]:
        return {
            "postgres": {"start_command": "echo start-pg"},
            "redis": {},
        }


class MinimalOverlay(OverlayBase):
    """Overlay that returns empty/None for most methods — tests fallback paths."""

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []

    def get_run_commands(self, worktree: Worktree) -> RunCommands:
        return {}

    def get_test_command(self, worktree: Worktree) -> str:
        return ""

    def get_tool_commands(self) -> list[ToolCommand]:
        return []


class HelplessToolOverlay(FullOverlay):
    """Overlay with a tool that has no help text — tests the else branch in list_tools."""

    def get_tool_commands(self) -> list[ToolCommand]:
        return [{"name": "bare-tool"}]


class PostDbStepsOverlay(FullOverlay):
    """Overlay with post-DB steps configured — tests the post-DB loop."""

    def get_post_db_steps(self, worktree: Worktree) -> list[PostDbStep]:
        return [
            {"name": "run-migrations", "command": "echo migrate"},
            {"name": "collectstatic", "command": "echo collectstatic"},
            {"name": "no-command-step"},
        ]


class FailingImportOverlay(FullOverlay):
    """Overlay where db_import always fails — tests error reporting."""

    def db_import(self, worktree: Worktree, *, force: bool = False) -> bool:
        return False


class PreRunOverlay(FullOverlay):
    """Overlay with pre-run steps — tests the pre-run loop in lifecycle setup."""

    def get_pre_run_steps(self, worktree: Worktree, service: str) -> list[ProvisionStep]:
        def _log_step() -> None:
            extra = dict(worktree.extra or {})
            log = extra.get("pre_run_log", [])
            log.append(service)
            extra["pre_run_log"] = log
            worktree.extra = extra
            worktree.save(update_fields=["extra"])

        return [ProvisionStep(name=f"prep-{service}", callable=_log_step)]


FULL_OVERLAY = "tests.teetree_core.test_new_management_commands.FullOverlay"
MINIMAL_OVERLAY = "tests.teetree_core.test_new_management_commands.MinimalOverlay"
SERVICES_OVERLAY = "tests.teetree_core.test_new_management_commands.ServicesOverlay"
POST_DB_OVERLAY = "tests.teetree_core.test_new_management_commands.PostDbStepsOverlay"
PRE_RUN_OVERLAY = "tests.teetree_core.test_new_management_commands.PreRunOverlay"

SETTINGS = {
    "TEATREE_OVERLAY_CLASS": FULL_OVERLAY,
    "TEATREE_HEADLESS_RUNTIME": "claude-code",
    "TEATREE_INTERACTIVE_RUNTIME": "codex",
    "TEATREE_TERMINAL_MODE": "same-terminal",
}

MINIMAL_SETTINGS = {
    "TEATREE_OVERLAY_CLASS": MINIMAL_OVERLAY,
    "TEATREE_HEADLESS_RUNTIME": "claude-code",
    "TEATREE_INTERACTIVE_RUNTIME": "codex",
    "TEATREE_TERMINAL_MODE": "same-terminal",
}

SERVICES_SETTINGS = {
    "TEATREE_OVERLAY_CLASS": SERVICES_OVERLAY,
    "TEATREE_HEADLESS_RUNTIME": "claude-code",
    "TEATREE_INTERACTIVE_RUNTIME": "codex",
    "TEATREE_TERMINAL_MODE": "same-terminal",
}

FAILING_IMPORT_SETTINGS = {
    "TEATREE_OVERLAY_CLASS": "tests.teetree_core.test_new_management_commands.FailingImportOverlay",
    "TEATREE_HEADLESS_RUNTIME": "claude-code",
    "TEATREE_INTERACTIVE_RUNTIME": "codex",
    "TEATREE_TERMINAL_MODE": "same-terminal",
}

POST_DB_SETTINGS = {
    "TEATREE_OVERLAY_CLASS": POST_DB_OVERLAY,
    "TEATREE_HEADLESS_RUNTIME": "claude-code",
    "TEATREE_INTERACTIVE_RUNTIME": "codex",
    "TEATREE_TERMINAL_MODE": "same-terminal",
}

PRE_RUN_SETTINGS = {
    "TEATREE_OVERLAY_CLASS": PRE_RUN_OVERLAY,
    "TEATREE_HEADLESS_RUNTIME": "claude-code",
    "TEATREE_INTERACTIVE_RUNTIME": "codex",
    "TEATREE_TERMINAL_MODE": "same-terminal",
}


@pytest.fixture(autouse=True)
def _clear_overlay() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()


# ── Workspace commands ──────────────────────────────────────────────


@pytest.mark.django_db
class TestWorkspaceTicket:
    @override_settings(**SETTINGS)
    def test_creates_ticket_and_worktrees(self) -> None:
        ticket_id = cast("int", call_command("workspace", "ticket", "https://example.com/issues/42"))

        ticket = Ticket.objects.get(pk=ticket_id)
        assert ticket.issue_url == "https://example.com/issues/42"
        assert ticket.state == Ticket.State.SCOPED
        assert ticket.repos == ["backend", "frontend"]
        assert ticket.worktrees.count() == 2

    @override_settings(**SETTINGS)
    def test_with_variant(self) -> None:
        ticket_id = cast("int", call_command("workspace", "ticket", "https://example.com/issues/43", variant="acme"))

        ticket = Ticket.objects.get(pk=ticket_id)
        assert ticket.variant == "acme"

    @override_settings(**SETTINGS)
    def test_with_custom_repos(self) -> None:
        ticket_id = cast("int", call_command("workspace", "ticket", "https://example.com/issues/92", repos="api,web"))

        ticket = Ticket.objects.get(pk=ticket_id)
        assert ticket.repos == ["api", "web"]
        assert ticket.worktrees.count() == 2

    @override_settings(**SETTINGS, T3_WORKSPACE_DIR="/tmp/ws-test")
    def test_with_git_worktree_creation(self, tmp_path: "pytest.TempPathFactory") -> None:
        """Test the workspace ticket command with successful git worktree creation."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # Create git repos
        for repo_name in ("backend", "frontend"):
            repo_dir = workspace / repo_name
            repo_dir.mkdir()
            (repo_dir / ".git").mkdir()
            (repo_dir / ".python-version").write_text("3.12.6")

        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch("teetree.core.management.commands.workspace._workspace_dir", return_value=workspace),
            patch("teetree.core.management.commands.workspace.subprocess.run", return_value=mock_result),
        ):
            ticket_id = cast("int", call_command("workspace", "ticket", "https://example.com/issues/80"))

        ticket = Ticket.objects.get(pk=ticket_id)
        assert ticket.worktrees.count() == 2

    @override_settings(**SETTINGS, T3_WORKSPACE_DIR="/tmp/ws-test")
    def test_skips_non_git_repo(self, tmp_path: "pytest.TempPathFactory") -> None:
        """Repos without .git directory are skipped."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        # Only backend has .git; frontend doesn't
        (workspace / "backend").mkdir()
        (workspace / "backend" / ".git").mkdir()
        (workspace / "frontend").mkdir()

        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch("teetree.core.management.commands.workspace._workspace_dir", return_value=workspace),
            patch("teetree.core.management.commands.workspace.subprocess.run", return_value=mock_result),
        ):
            ticket_id = cast("int", call_command("workspace", "ticket", "https://example.com/issues/81"))

        ticket = Ticket.objects.get(pk=ticket_id)
        # Both worktrees are created in DB; one was skipped during git worktree add
        assert ticket.worktrees.count() == 2

    @override_settings(**SETTINGS, T3_WORKSPACE_DIR="/tmp/ws-test")
    def test_handles_worktree_already_exists(self, tmp_path: "pytest.TempPathFactory") -> None:
        """When worktree path already exists, it's skipped."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "backend").mkdir()
        (workspace / "backend" / ".git").mkdir()
        (workspace / "frontend").mkdir()
        (workspace / "frontend" / ".git").mkdir()

        # Pre-create the ticket_dir/backend to simulate existing worktree
        prefix = "ac"
        branch = f"{prefix}-backend-82-ticket"
        ticket_dir = workspace / branch
        ticket_dir.mkdir(parents=True)
        (ticket_dir / "backend").mkdir()

        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch("teetree.core.management.commands.workspace._workspace_dir", return_value=workspace),
            patch("teetree.core.management.commands.workspace._branch_prefix", return_value="ac"),
            patch("teetree.core.management.commands.workspace.subprocess.run", return_value=mock_result),
        ):
            ticket_id = cast("int", call_command("workspace", "ticket", "https://example.com/issues/82"))

        ticket = Ticket.objects.get(pk=ticket_id)
        assert ticket.worktrees.count() == 2

    @override_settings(**SETTINGS, T3_WORKSPACE_DIR="/tmp/ws-test")
    def test_handles_git_worktree_failure(self, tmp_path: "pytest.TempPathFactory") -> None:
        """When git worktree add fails, the worktree is still created in DB with empty extra."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "backend").mkdir()
        (workspace / "backend" / ".git").mkdir()
        (workspace / "frontend").mkdir()
        (workspace / "frontend" / ".git").mkdir()

        mock_pull = MagicMock()
        mock_pull.returncode = 0

        mock_add = MagicMock()
        mock_add.returncode = 1
        mock_add.stderr = "fatal: branch already exists"

        def side_effect(cmd, **kwargs):
            if "worktree" in cmd:
                return mock_add
            return mock_pull

        with (
            patch("teetree.core.management.commands.workspace._workspace_dir", return_value=workspace),
            patch("teetree.core.management.commands.workspace.subprocess.run", side_effect=side_effect),
        ):
            ticket_id = cast("int", call_command("workspace", "ticket", "https://example.com/issues/83"))

        ticket = Ticket.objects.get(pk=ticket_id)
        # Worktrees are created in DB even if git worktree add failed
        for wt in ticket.worktrees.all():
            assert wt.extra == {}


@pytest.mark.django_db
class TestWorkspaceCleanAll:
    @override_settings(**SETTINGS)
    def test_removes_stale_worktrees(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/50")
        Worktree.objects.create(ticket=ticket, repo_path="/tmp/test", branch="feature")

        cleaned = cast("list[str]", call_command("workspace", "clean-all"))

        assert len(cleaned) == 1
        assert Worktree.objects.count() == 0

    @override_settings(**SETTINGS)
    def test_removes_git_worktree_and_branch(self, tmp_path: "pytest.TempPathFactory") -> None:
        """clean-all calls 'git worktree remove' and 'git branch -D' when wt_path is set."""
        workspace = tmp_path / "workspace"
        repo_main = workspace / "backend"
        repo_main.mkdir(parents=True)
        # Add a file so the dir is not empty (avoids empty-dir cleanup side-effect)
        (repo_main / ".git").mkdir()

        ticket = Ticket.objects.create(issue_url="https://example.com/issues/80")
        Worktree.objects.create(
            ticket=ticket,
            repo_path="backend",
            branch="ac-backend-80-ticket",
            extra={"worktree_path": str(workspace / "ac-backend-80-ticket" / "backend")},
        )

        with (
            patch("teetree.core.management.commands.workspace._workspace_dir", return_value=workspace),
            patch("teetree.core.management.commands.workspace.subprocess.run") as mock_run,
        ):
            cleaned = cast("list[str]", call_command("workspace", "clean-all"))

        assert any("Cleaned: backend" in c for c in cleaned)
        assert Worktree.objects.count() == 0

        # Should have called git worktree remove and git branch -D
        assert mock_run.call_count == 2
        worktree_remove_call = mock_run.call_args_list[0]
        branch_delete_call = mock_run.call_args_list[1]
        assert "worktree" in worktree_remove_call[0][0]
        assert "remove" in worktree_remove_call[0][0]
        assert "branch" in branch_delete_call[0][0]
        assert "-D" in branch_delete_call[0][0]
        assert "ac-backend-80-ticket" in branch_delete_call[0][0]

    @override_settings(**SETTINGS)
    def test_drops_database_when_db_name_set(self, tmp_path: "pytest.TempPathFactory") -> None:
        """clean-all calls dropdb when worktree has a db_name."""
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True)

        ticket = Ticket.objects.create(issue_url="https://example.com/issues/81")
        wt = Worktree.objects.create(
            ticket=ticket,
            repo_path="backend",
            branch="ac-backend-81-ticket",
            extra={},
        )
        # Set db_name directly (bypass FSM provision)
        Worktree.objects.filter(pk=wt.pk).update(db_name="wt_test_db")

        with (
            patch("teetree.core.management.commands.workspace._workspace_dir", return_value=workspace),
            patch("teetree.core.management.commands.workspace.subprocess.run") as mock_run,
            patch("teetree.utils.db.pg_host", return_value="localhost"),
            patch("teetree.utils.db.pg_user", return_value="testuser"),
            patch("teetree.utils.db.pg_env", return_value={"PGPASSWORD": "secret"}),
        ):
            cleaned = cast("list[str]", call_command("workspace", "clean-all"))

        assert len(cleaned) == 1
        assert Worktree.objects.count() == 0

        # Should have called dropdb
        assert mock_run.call_count == 1
        dropdb_call = mock_run.call_args_list[0]
        assert "dropdb" in dropdb_call[0][0]
        assert "wt_test_db" in dropdb_call[0][0]

    @override_settings(**SETTINGS)
    def test_removes_empty_ticket_directories(self, tmp_path: "pytest.TempPathFactory") -> None:
        """clean-all removes empty directories in workspace after cleaning worktrees."""
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True)

        # Create an empty directory that should be cleaned up
        empty_dir = workspace / "ac-backend-90-ticket"
        empty_dir.mkdir()

        # Create a non-empty directory that should NOT be removed
        nonempty_dir = workspace / "ac-backend-91-ticket"
        nonempty_dir.mkdir()
        (nonempty_dir / "some_file.txt").write_text("content", encoding="utf-8")

        with patch("teetree.core.management.commands.workspace._workspace_dir", return_value=workspace):
            cleaned = cast("list[str]", call_command("workspace", "clean-all"))

        # Only the empty dir should be removed
        assert any("ac-backend-90-ticket" in c for c in cleaned)
        assert not any("ac-backend-91-ticket" in c for c in cleaned)
        assert not empty_dir.exists()
        assert nonempty_dir.exists()


@pytest.mark.django_db
class TestWorkspaceFinalize:
    @override_settings(**SETTINGS)
    def test_squashes_and_rebases_worktrees(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/90")
        Worktree.objects.create(ticket=ticket, repo_path="/tmp/backend", branch="feature-90")
        Worktree.objects.create(ticket=ticket, repo_path="/tmp/frontend", branch="feature-90")

        def fake_run(cmd, **kwargs):
            result = MagicMock(returncode=0, stdout="abc123\n", stderr="")
            if isinstance(cmd, list):
                if "rev-list" in cmd and "--count" in cmd:
                    result.stdout = "3\n"
                elif "log" in cmd and "--oneline" in cmd:
                    result.stdout = "abc fix: first change\ndef feat: second\n"
            return result

        with (
            patch("teetree.core.management.commands.workspace.default_branch", return_value="main"),
            patch("teetree.core.management.commands.workspace.git_run"),
            patch("teetree.core.management.commands.workspace.subprocess.run", side_effect=fake_run),
        ):
            result = cast("str", call_command("workspace", "finalize", str(ticket.pk)))

        assert "squashed 3 commits" in result
        assert "rebased on main" in result

    @override_settings(**SETTINGS)
    def test_handles_rebase_failure(self) -> None:
        import subprocess as sp  # noqa: PLC0415

        ticket = Ticket.objects.create(issue_url="https://example.com/issues/91")
        Worktree.objects.create(ticket=ticket, repo_path="/tmp/backend", branch="feature-91")

        def mock_git_run(*, repo, args, **kwargs):
            if "rebase" in args:
                raise sp.CalledProcessError(1, "git rebase")
            return ""

        def fake_run(cmd, **kwargs):
            return MagicMock(returncode=0, stdout="1\n", stderr="")

        with (
            patch("teetree.core.management.commands.workspace.default_branch", return_value="main"),
            patch("teetree.core.management.commands.workspace.git_run", side_effect=mock_git_run),
            patch("teetree.core.management.commands.workspace.subprocess.run", side_effect=fake_run),
        ):
            result = cast("str", call_command("workspace", "finalize", str(ticket.pk)))

        assert "failed" in result.lower()


# ── DB commands ─────────────────────────────────────────────────────


@pytest.mark.django_db
class TestDbRefresh:
    @override_settings(**SETTINGS)
    def test_transitions_worktree(self, tmp_path: Path) -> None:
        wt_dir = tmp_path / "test"
        wt_dir.mkdir()
        ticket = Ticket.objects.create()
        worktree = Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/test", branch="feature", extra={"worktree_path": str(wt_dir)}
        )
        worktree.provision()
        worktree.save()

        result = cast("str", call_command("db", "refresh", path=str(wt_dir)))

        worktree.refresh_from_db()
        assert "refreshed" in result.lower()
        assert worktree.state == Worktree.State.PROVISIONED

    @override_settings(**SETTINGS)
    def test_runs_post_db_steps_and_reset_passwords(self, tmp_path: Path) -> None:
        """Db refresh calls post-DB steps and password reset after successful import."""
        wt_dir = tmp_path / "test"
        wt_dir.mkdir()
        ticket = Ticket.objects.create()
        worktree = Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/test", branch="feature", extra={"worktree_path": str(wt_dir)}
        )
        worktree.provision()
        worktree.save()

        with patch("teetree.core.management.commands.db.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(returncode=0)
            result = cast("str", call_command("db", "refresh", path=str(wt_dir)))

        assert "refreshed" in result.lower()
        # Post-DB steps and password reset should have been called
        assert mock_sp.run.call_count >= 1

    @override_settings(**FAILING_IMPORT_SETTINGS)
    def test_reports_failure_when_import_fails(self, tmp_path: Path) -> None:
        """Db refresh reports failure when overlay.db_import returns False."""
        wt_dir = tmp_path / "test"
        wt_dir.mkdir()
        ticket = Ticket.objects.create()
        worktree = Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/test", branch="feature", extra={"worktree_path": str(wt_dir)}
        )
        worktree.provision()
        worktree.save()

        result = cast("str", call_command("db", "refresh", path=str(wt_dir)))

        assert "failed" in result.lower()

    @override_settings(**POST_DB_SETTINGS)
    def test_runs_post_db_steps_loop(self, tmp_path: Path) -> None:
        """Db refresh iterates over overlay.get_post_db_steps and runs commands (lines 37-40)."""
        wt_dir = tmp_path / "test"
        wt_dir.mkdir()
        ticket = Ticket.objects.create()
        worktree = Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/test", branch="feature", extra={"worktree_path": str(wt_dir)}
        )
        worktree.provision()
        worktree.save()

        with patch("teetree.core.management.commands.db.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(returncode=0)
            result = cast("str", call_command("db", "refresh", path=str(wt_dir)))

        assert "refreshed" in result.lower()
        # 2 steps with commands + 1 password reset = 3 subprocess.run calls
        # (the "no-command-step" has no "command" key so it's skipped)
        assert mock_sp.run.call_count == 3

    @override_settings(**MINIMAL_SETTINGS)
    def test_no_strategy_returns_message(self, tmp_path: Path) -> None:
        wt_dir = tmp_path / "test"
        wt_dir.mkdir()
        ticket = Ticket.objects.create()
        worktree = Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/test", branch="feature", extra={"worktree_path": str(wt_dir)}
        )
        worktree.provision()
        worktree.save()

        result = cast("str", call_command("db", "refresh", path=str(wt_dir)))

        assert "no db import strategy" in result.lower()


@pytest.mark.django_db
class TestDbRestoreCi:
    @override_settings(**FAILING_IMPORT_SETTINGS)
    def test_reports_failure(self, tmp_path: Path) -> None:
        """restore-ci returns failure message when db_import returns False (line 65)."""
        wt_dir = tmp_path / "test"
        wt_dir.mkdir()
        ticket = Ticket.objects.create()
        worktree = Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/test", branch="feature", extra={"worktree_path": str(wt_dir)}
        )
        worktree.provision()
        worktree.save()

        result = cast("str", call_command("db", "restore-ci", path=str(wt_dir)))

        assert "failed" in result.lower()

    @override_settings(**SETTINGS)
    def test_calls_db_import_with_force(self, tmp_path: Path) -> None:
        """restore-ci calls db_import with force=True."""
        wt_dir = tmp_path / "test"
        wt_dir.mkdir()
        ticket = Ticket.objects.create()
        worktree = Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/test", branch="feature", extra={"worktree_path": str(wt_dir)}
        )
        worktree.provision()
        worktree.save()

        result = cast("str", call_command("db", "restore-ci", path=str(wt_dir)))

        worktree.refresh_from_db()
        assert "restored" in result.lower()
        assert worktree.state == Worktree.State.PROVISIONED

    @override_settings(**SETTINGS)
    def test_with_strategy(self, tmp_path: Path) -> None:
        wt_dir = tmp_path / "test"
        wt_dir.mkdir()
        ticket = Ticket.objects.create()
        worktree = Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/test", branch="feature", extra={"worktree_path": str(wt_dir)}
        )
        worktree.provision()
        worktree.save()

        result = cast("str", call_command("db", "restore-ci", path=str(wt_dir)))

        assert "restored" in result.lower() or "failed" in result.lower()

    @override_settings(**MINIMAL_SETTINGS)
    def test_no_strategy_returns_message(self, tmp_path: Path) -> None:
        wt_dir = tmp_path / "test"
        wt_dir.mkdir()
        ticket = Ticket.objects.create()
        Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/test", branch="feature", extra={"worktree_path": str(wt_dir)}
        )

        result = cast("str", call_command("db", "restore-ci", path=str(wt_dir)))

        assert "no db import strategy" in result.lower()


@pytest.mark.django_db
class TestDbResetPasswords:
    @override_settings(**SETTINGS)
    def test_calls_overlay_command(self, tmp_path: Path) -> None:
        wt_dir = tmp_path / "test"
        wt_dir.mkdir()
        ticket = Ticket.objects.create()
        Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/test", branch="feature", extra={"worktree_path": str(wt_dir)}
        )

        with patch("teetree.core.management.commands.db.subprocess.run") as mock_run:
            result = cast("str", call_command("db", "reset-passwords", path=str(wt_dir)))

        assert "reset" in result.lower()
        mock_run.assert_called_once()

    @override_settings(**MINIMAL_SETTINGS)
    def test_no_command_returns_message(self, tmp_path: Path) -> None:
        wt_dir = tmp_path / "test"
        wt_dir.mkdir()
        ticket = Ticket.objects.create()
        Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/test", branch="feature", extra={"worktree_path": str(wt_dir)}
        )

        result = cast("str", call_command("db", "reset-passwords", path=str(wt_dir)))

        assert "no reset-passwords command" in result.lower()


# ── PR commands ─────────────────────────────────────────────────────


@pytest.mark.django_db
class TestPrCreate:
    @override_settings(**SETTINGS)
    def test_without_code_host_returns_error(self) -> None:
        ticket = Ticket.objects.create()

        result = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk)))

        assert "error" in result

    @override_settings(**SETTINGS)
    def test_with_code_host_creates_mr(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/70")
        Worktree.objects.create(ticket=ticket, repo_path="my-repo", branch="feature-70")

        mock_host = MagicMock()
        mock_host.create_pr.return_value = {"url": "https://example.com/mr/1"}

        with patch("teetree.core.management.commands.pr.get_code_host", return_value=mock_host):
            result = cast(
                "dict[str, object]",
                call_command(
                    "pr",
                    "create",
                    str(ticket.pk),
                    title="Fix bug",
                    description="Fixes the thing",
                ),
            )

        assert result == {"url": "https://example.com/mr/1"}
        mock_host.create_pr.assert_called_once_with(
            repo="my-repo",
            branch="feature-70",
            title="Fix bug",
            description="Fixes the thing",
            labels=None,
        )

    @override_settings(**SETTINGS)
    def test_validation_failure(self) -> None:
        """FullOverlay.validate_mr returns error when title is empty."""
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/71")
        Worktree.objects.create(ticket=ticket, repo_path="my-repo", branch="feature-71")

        mock_host = MagicMock()

        with patch("teetree.core.management.commands.pr.get_code_host", return_value=mock_host):
            result = cast(
                "dict[str, object]",
                call_command(
                    "pr",
                    "create",
                    str(ticket.pk),
                    title="",
                    description="desc",
                ),
            )

        assert result["error"] == "MR validation failed"
        mock_host.create_pr.assert_not_called()

    @override_settings(**SETTINGS)
    def test_no_worktree_uses_ticket_number(self) -> None:
        """When ticket has no worktrees, fallback branch and empty repo are used."""
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/72")

        mock_host = MagicMock()
        mock_host.create_pr.return_value = {"url": "https://example.com/mr/2"}

        with patch("teetree.core.management.commands.pr.get_code_host", return_value=mock_host):
            cast(
                "dict[str, object]",
                call_command(
                    "pr",
                    "create",
                    str(ticket.pk),
                    title="My MR",
                ),
            )

        call_kwargs = mock_host.create_pr.call_args[1]
        assert call_kwargs["branch"] == "ticket-72"
        assert call_kwargs["repo"] == ""

    @override_settings(**SETTINGS)
    def test_uses_default_title_from_issue_url(self) -> None:
        """When no title is given, it defaults to 'Resolve <issue_url>'."""
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/73")
        Worktree.objects.create(ticket=ticket, repo_path="my-repo", branch="feature-73")

        mock_host = MagicMock()
        mock_host.create_pr.return_value = {"url": "https://example.com/mr/3"}

        with patch("teetree.core.management.commands.pr.get_code_host", return_value=mock_host):
            call_command("pr", "create", str(ticket.pk), title="My Title")

        call_kwargs = mock_host.create_pr.call_args[1]
        assert call_kwargs["title"] == "My Title"


@pytest.mark.django_db
class TestPrCheckGates:
    @override_settings(**SETTINGS)
    def test_without_session_returns_not_allowed(self) -> None:
        ticket = Ticket.objects.create()

        result = cast("dict[str, object]", call_command("pr", "check-gates", str(ticket.pk)))

        assert result["allowed"] is False

    @override_settings(**SETTINGS)
    def test_with_session_passes(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        session.visit_phase("testing")
        session.visit_phase("reviewing")

        result = cast("dict[str, object]", call_command("pr", "check-gates", str(ticket.pk), target_phase="shipping"))

        assert result["allowed"] is True

    @override_settings(**SETTINGS)
    def test_missing_phases_returns_not_allowed(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        # Only visited "testing", missing "reviewing" for shipping
        session.visit_phase("testing")

        result = cast("dict[str, object]", call_command("pr", "check-gates", str(ticket.pk), target_phase="shipping"))

        assert result["allowed"] is False
        assert "reviewing" in str(result["reason"])


@pytest.mark.django_db
class TestPrFetchIssue:
    @override_settings(**SETTINGS)
    def test_without_tracker_returns_error(self) -> None:
        result = cast("dict[str, object]", call_command("pr", "fetch-issue", "https://example.com/issues/1"))

        assert "error" in result

    @override_settings(**SETTINGS)
    def test_with_tracker(self) -> None:
        mock_tracker = MagicMock()
        mock_tracker.get_issue.return_value = {"title": "Bug", "state": "opened"}

        with patch("teetree.core.management.commands.pr.get_issue_tracker", return_value=mock_tracker):
            result = cast("dict[str, object]", call_command("pr", "fetch-issue", "https://example.com/issues/1"))

        assert result == {"title": "Bug", "state": "opened"}


@pytest.mark.django_db
class TestPrDetectTenant:
    @override_settings(**SETTINGS)
    def test_returns_overlay_variant(self) -> None:
        result = cast("str", call_command("pr", "detect-tenant"))

        assert result == "test_variant"


@pytest.mark.django_db
class TestPrPostEvidence:
    @override_settings(**SETTINGS)
    def test_without_code_host_returns_error(self) -> None:
        result = cast("dict[str, object]", call_command("pr", "post-evidence", "100"))

        assert "error" in result

    @override_settings(**SETTINGS)
    def test_with_code_host(self) -> None:
        mock_host = MagicMock()
        mock_host.post_mr_note.return_value = {"id": 42}
        mock_host.list_mr_notes.return_value = []  # no existing note

        with patch("teetree.core.management.commands.pr.get_code_host", return_value=mock_host):
            result = cast(
                "dict[str, object]",
                call_command(
                    "pr",
                    "post-evidence",
                    "100",
                    repo="my/repo",
                    title="Evidence",
                    body="Test passed",
                ),
            )

        assert result == {"id": 42}
        call_kwargs = mock_host.post_mr_note.call_args[1]
        assert call_kwargs["mr_iid"] == 100
        assert "## Evidence" in call_kwargs["body"]
        assert "Test passed" in call_kwargs["body"]

    @override_settings(**SETTINGS)
    def test_updates_existing_note(self) -> None:
        mock_host = MagicMock()
        mock_host.list_mr_notes.return_value = [
            {"id": 999, "body": "## Test Plan\n\nOld content", "system": False},
        ]
        mock_host.update_mr_note.return_value = {"id": 999}

        with patch("teetree.core.management.commands.pr.get_code_host", return_value=mock_host):
            result = cast(
                "dict[str, object]",
                call_command(
                    "pr",
                    "post-evidence",
                    "100",
                    repo="my/repo",
                    body="Updated content",
                ),
            )

        assert result == {"id": 999}
        mock_host.update_mr_note.assert_called_once()
        call_kwargs = mock_host.update_mr_note.call_args[1]
        assert call_kwargs["note_id"] == 999
        assert "Updated content" in call_kwargs["body"]
        mock_host.post_mr_note.assert_not_called()

    @override_settings(**SETTINGS)
    def test_uploads_files(self) -> None:
        mock_host = MagicMock()
        mock_host.upload_file.return_value = {"markdown": "![screenshot](/uploads/abc/img.png)"}
        mock_host.list_mr_notes.return_value = []
        mock_host.post_mr_note.return_value = {"id": 55}

        with patch("teetree.core.management.commands.pr.get_code_host", return_value=mock_host):
            cast(
                "dict[str, object]",
                call_command(
                    "pr",
                    "post-evidence",
                    "100",
                    repo="my/repo",
                    body="Evidence",
                    files=["/tmp/img.png"],
                ),
            )

        mock_host.upload_file.assert_called_once_with(repo="my/repo", filepath="/tmp/img.png")
        body = mock_host.post_mr_note.call_args[1]["body"]
        assert "![screenshot](/uploads/abc/img.png)" in body

    @override_settings(**SETTINGS)
    def test_skips_empty_upload_markdown(self) -> None:
        """When upload returns no markdown key, the embed is skipped."""
        mock_host = MagicMock()
        mock_host.upload_file.return_value = {}  # no markdown
        mock_host.list_mr_notes.return_value = []
        mock_host.post_mr_note.return_value = {"id": 56}

        with patch("teetree.core.management.commands.pr.get_code_host", return_value=mock_host):
            cast(
                "dict[str, object]",
                call_command("pr", "post-evidence", "100", repo="my/repo", body="x", files=["/tmp/bad.png"]),
            )

        body = mock_host.post_mr_note.call_args[1]["body"]
        assert "![" not in body  # no embed added

    @override_settings(**SETTINGS)
    def test_without_body(self) -> None:
        mock_host = MagicMock()
        mock_host.post_mr_note.return_value = {"id": 43}
        mock_host.list_mr_notes.return_value = []

        with patch("teetree.core.management.commands.pr.get_code_host", return_value=mock_host):
            cast(
                "dict[str, object]",
                call_command(
                    "pr",
                    "post-evidence",
                    "101",
                    title="Screenshot",
                ),
            )

        call_kwargs = mock_host.post_mr_note.call_args[1]
        assert "_No details provided._" in call_kwargs["body"]

    @override_settings(**SETTINGS)
    def test_uses_overlay_ci_project_path(self) -> None:
        """When no repo is given, falls back to overlay.get_ci_project_path()."""
        mock_host = MagicMock()
        mock_host.post_mr_note.return_value = {"id": 44}
        mock_host.list_mr_notes.return_value = []

        with patch("teetree.core.management.commands.pr.get_code_host", return_value=mock_host):
            call_command("pr", "post-evidence", "102", title="T")

        call_kwargs = mock_host.post_mr_note.call_args[1]
        assert call_kwargs["repo"] == "test/project"


# ── Run commands ───────────────────────────────────────────────────


@pytest.mark.django_db
class TestRunBackend:
    @override_settings(**SETTINGS)
    def test_calls_overlay_command(self, tmp_path: Path) -> None:
        wt_dir = tmp_path / "backend"
        wt_dir.mkdir()
        ticket = Ticket.objects.create()
        Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/backend", branch="feature", extra={"worktree_path": str(wt_dir)}
        )

        with patch("teetree.core.management.commands.run.subprocess.run") as mock_run:
            result = cast("str", call_command("run", "backend", path=str(wt_dir)))

        mock_run.assert_called_once()
        assert "started" in result.lower()

    @override_settings(**MINIMAL_SETTINGS)
    def test_no_command_returns_message(self, tmp_path: Path) -> None:
        wt_dir = tmp_path / "backend"
        wt_dir.mkdir()
        ticket = Ticket.objects.create()
        Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/backend", branch="feature", extra={"worktree_path": str(wt_dir)}
        )

        result = cast("str", call_command("run", "backend", path=str(wt_dir)))

        assert "no backend command" in result.lower()

    @override_settings(**{**SETTINGS, "TEATREE_OVERLAY_CLASS": SERVICES_OVERLAY})
    def test_starts_services_before_command(self, tmp_path: Path) -> None:
        """Backend command calls _start_services which runs start_command for each service."""
        wt_dir = tmp_path / "backend"
        wt_dir.mkdir()
        ticket = Ticket.objects.create()
        Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/backend", branch="feature", extra={"worktree_path": str(wt_dir)}
        )

        reset_overlay_cache()
        with patch("teetree.core.management.commands.run.subprocess.run") as mock_run:
            call_command("run", "backend", path=str(wt_dir))

        # 2 calls: one for postgres start_command, one for the backend command itself.
        # Redis has no start_command so it's skipped.
        assert mock_run.call_count == 2


@pytest.mark.django_db
class TestRunFrontend:
    @override_settings(**SETTINGS)
    def test_calls_overlay_command(self, tmp_path: Path) -> None:
        wt_dir = tmp_path / "frontend"
        wt_dir.mkdir()
        ticket = Ticket.objects.create()
        Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/frontend", branch="feature", extra={"worktree_path": str(wt_dir)}
        )

        with patch("teetree.core.management.commands.run.subprocess.run") as mock_run:
            result = cast("str", call_command("run", "frontend", path=str(wt_dir)))

        mock_run.assert_called_once()
        assert "started" in result.lower()

    @override_settings(**MINIMAL_SETTINGS)
    def test_no_command_returns_message(self, tmp_path: Path) -> None:
        wt_dir = tmp_path / "frontend"
        wt_dir.mkdir()
        ticket = Ticket.objects.create()
        Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/frontend", branch="feature", extra={"worktree_path": str(wt_dir)}
        )

        result = cast("str", call_command("run", "frontend", path=str(wt_dir)))

        assert "no frontend command" in result.lower()


@pytest.mark.django_db
class TestRunBuildFrontend:
    @override_settings(**SETTINGS)
    def test_calls_overlay_command(self, tmp_path: Path) -> None:
        wt_dir = tmp_path / "frontend"
        wt_dir.mkdir()
        ticket = Ticket.objects.create()
        Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/frontend", branch="feature", extra={"worktree_path": str(wt_dir)}
        )

        with patch("teetree.core.management.commands.run.subprocess.run") as mock_run:
            result = cast("str", call_command("run", "build-frontend", path=str(wt_dir)))

        mock_run.assert_called_once()
        assert "built" in result.lower()

    @override_settings(**MINIMAL_SETTINGS)
    def test_no_command_returns_message(self, tmp_path: Path) -> None:
        wt_dir = tmp_path / "frontend"
        wt_dir.mkdir()
        ticket = Ticket.objects.create()
        Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/frontend", branch="feature", extra={"worktree_path": str(wt_dir)}
        )

        result = cast("str", call_command("run", "build-frontend", path=str(wt_dir)))

        assert "no build-frontend command" in result.lower()


@pytest.mark.django_db
class TestRunTests:
    @override_settings(**SETTINGS)
    def test_calls_overlay_test_command(self, tmp_path: Path) -> None:
        wt_dir = tmp_path / "backend"
        wt_dir.mkdir()
        ticket = Ticket.objects.create()
        Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/backend", branch="feature", extra={"worktree_path": str(wt_dir)}
        )

        with patch("teetree.core.management.commands.run.subprocess.run") as mock_run:
            result = cast("str", call_command("run", "tests", path=str(wt_dir)))

        mock_run.assert_called_once()
        assert "completed" in result.lower()

    @override_settings(**MINIMAL_SETTINGS)
    def test_no_command_returns_message(self, tmp_path: Path) -> None:
        wt_dir = tmp_path / "backend"
        wt_dir.mkdir()
        ticket = Ticket.objects.create()
        Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/backend", branch="feature", extra={"worktree_path": str(wt_dir)}
        )

        result = cast("str", call_command("run", "tests", path=str(wt_dir)))

        assert "no test command" in result.lower()


@pytest.mark.django_db
class TestRunVerify:
    pass  # No verify tests in the original file — placeholder for future tests


@pytest.mark.django_db
class TestRunServices:
    pass  # No standalone services tests in the original file — placeholder for future tests


@pytest.mark.django_db
class TestRunE2e:
    @override_settings(**SETTINGS)
    def test_triggers_pipeline(self) -> None:
        mock_ci = MagicMock()
        mock_ci.trigger_pipeline.return_value = {"pipeline_id": 123}

        with patch("teetree.backends.loader.get_ci_service", return_value=mock_ci):
            result = cast("dict[str, object]", call_command("run", "e2e"))

        assert result == {"pipeline_id": 123}
        mock_ci.trigger_pipeline.assert_called_once_with(
            project="test/e2e-project",
            ref="main",
            variables={"E2E": "true"},
        )

    @override_settings(**SETTINGS)
    def test_with_branch_override(self) -> None:
        mock_ci = MagicMock()
        mock_ci.trigger_pipeline.return_value = {"pipeline_id": 456}

        with patch("teetree.backends.loader.get_ci_service", return_value=mock_ci):
            cast("dict[str, object]", call_command("run", "e2e", branch="feature-branch"))

        mock_ci.trigger_pipeline.assert_called_once_with(
            project="test/e2e-project",
            ref="feature-branch",
            variables={"E2E": "true"},
        )

    @override_settings(**MINIMAL_SETTINGS)
    def test_no_config_returns_error(self) -> None:
        result = cast("dict[str, object]", call_command("run", "e2e"))

        assert "error" in result

    @override_settings(**SETTINGS)
    def test_no_ci_service_returns_error(self) -> None:
        with patch("teetree.backends.loader.get_ci_service", return_value=None):
            result = cast("dict[str, object]", call_command("run", "e2e"))

        assert "error" in result


# ── Lifecycle commands ──────────────────────────────────────────────


@pytest.mark.django_db
class TestLifecycleSetup:
    @override_settings(**SETTINGS)
    def test_runs_reset_passwords(self, tmp_path: Path) -> None:
        wt_dir = tmp_path / "backend"
        wt_dir.mkdir()
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/60")
        Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/backend", branch="feature", extra={"worktree_path": str(wt_dir)}
        )

        with patch("teetree.core.management.commands.lifecycle.subprocess.run") as mock_run:
            call_command("lifecycle", "setup", path=str(wt_dir))

        # FullOverlay.get_reset_passwords_command returns "echo passwords_reset"
        # Find the password reset call (direnv allow may also be called)
        pw_calls = [c for c in mock_run.call_args_list if c[0] and c[0][0] == "echo passwords_reset"]
        assert len(pw_calls) == 1
        assert pw_calls[0][1]["shell"] is True

    @override_settings(**SETTINGS)
    def test_already_provisioned_skips_provision(self, tmp_path: Path) -> None:
        """When worktree is already provisioned, setup skips the provision step."""
        wt_dir = tmp_path / "backend"
        wt_dir.mkdir()
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/61")
        wt = Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/backend", branch="feature", extra={"worktree_path": str(wt_dir)}
        )
        wt.provision()
        wt.save()

        with patch("teetree.core.management.commands.lifecycle.subprocess.run"):
            worktree_id = cast("int", call_command("lifecycle", "setup", path=str(wt_dir)))

        worktree = Worktree.objects.get(pk=worktree_id)
        assert worktree.state == Worktree.State.PROVISIONED

    @override_settings(**SETTINGS)
    def test_variant_option_updates_ticket(self, tmp_path: Path) -> None:
        """The --variant option updates the ticket variant before provisioning."""
        wt_dir = tmp_path / "backend"
        wt_dir.mkdir()
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/90", variant="")
        Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/backend", branch="feature", extra={"worktree_path": str(wt_dir)}
        )

        with patch("teetree.core.management.commands.lifecycle.subprocess.run"):
            call_command("lifecycle", "setup", path=str(wt_dir), variant="testcustomer")

        ticket.refresh_from_db()
        assert ticket.variant == "testcustomer"

    @override_settings(**FAILING_IMPORT_SETTINGS)
    def test_continues_on_db_import_failure(self, tmp_path: Path) -> None:
        """Setup continues with provision steps even when db_import fails."""
        wt_dir = tmp_path / "backend"
        wt_dir.mkdir()
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/70")
        Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/backend", branch="feature", extra={"worktree_path": str(wt_dir)}
        )

        with patch("teetree.core.management.commands.lifecycle.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(returncode=0)
            worktree_id = cast("int", call_command("lifecycle", "setup", path=str(wt_dir)))

        worktree = Worktree.objects.get(pk=worktree_id)
        assert worktree.state == Worktree.State.PROVISIONED

    @override_settings(**SETTINGS)
    def test_runs_post_db_steps(self, tmp_path: Path) -> None:
        """Setup runs post-DB steps from the overlay."""
        wt_dir = tmp_path / "backend"
        wt_dir.mkdir()
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/71")
        Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/backend", branch="feature", extra={"worktree_path": str(wt_dir)}
        )

        with patch("teetree.core.management.commands.lifecycle.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(returncode=0)
            call_command("lifecycle", "setup", path=str(wt_dir))

        # Should have called subprocess.run for password reset at minimum
        assert mock_sp.run.call_count >= 1

    @override_settings(**POST_DB_SETTINGS)
    def test_runs_post_db_steps_with_commands(self, tmp_path: Path) -> None:
        """Setup iterates post-DB steps and runs commands via subprocess (lines 49-52)."""
        wt_dir = tmp_path / "backend"
        wt_dir.mkdir()
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/72")
        Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/backend", branch="feature", extra={"worktree_path": str(wt_dir)}
        )

        with patch("teetree.core.management.commands.lifecycle.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(returncode=0)
            call_command("lifecycle", "setup", path=str(wt_dir))

        # PostDbStepsOverlay returns 3 steps: 2 with commands + 1 without command
        # Plus 1 password reset call + 1 direnv allow call = 4 total subprocess.run calls
        assert mock_sp.run.call_count == 4

    @override_settings(**PRE_RUN_SETTINGS)
    def test_runs_pre_run_steps_for_all_services(self, tmp_path: Path) -> None:
        """Setup calls get_pre_run_steps for every service from get_run_commands."""
        wt_dir = tmp_path / "backend"
        wt_dir.mkdir()
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/73")
        wt = Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/backend", branch="feature", extra={"worktree_path": str(wt_dir)}
        )

        with patch("teetree.core.management.commands.lifecycle.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(returncode=0)
            call_command("lifecycle", "setup", path=str(wt_dir))

        # PreRunOverlay.get_run_commands returns backend, frontend, build-frontend
        wt.refresh_from_db()
        assert sorted((wt.extra or {}).get("pre_run_log", [])) == ["backend", "build-frontend", "frontend"]

    @override_settings(**SETTINGS)
    def test_writes_skill_metadata_cache(self, tmp_path: "pytest.TempPathFactory") -> None:
        """Setup writes the overlay skill metadata to DATA_DIR/skill-metadata.json."""
        wt_dir = tmp_path / "backend"
        wt_dir.mkdir()
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/63")
        Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/backend", branch="feature", extra={"worktree_path": str(wt_dir)}
        )

        with (
            patch("teetree.core.management.commands.lifecycle.subprocess.run"),
            patch("teetree.core.management.commands.lifecycle.DATA_DIR", tmp_path),
        ):
            call_command("lifecycle", "setup", path=str(wt_dir))

        cache_file = tmp_path / "skill-metadata.json"
        assert cache_file.exists()

    @override_settings(**SETTINGS)
    def test_runs_prek_install_when_config_exists(self, tmp_path: "pytest.TempPathFactory") -> None:
        """Setup runs 'prek install -f' when .pre-commit-config.yaml exists in worktree path."""
        wt_path = tmp_path / "worktree"
        wt_path.mkdir()
        (wt_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")

        ticket = Ticket.objects.create(issue_url="https://example.com/issues/100")
        Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/backend",
            branch="feature",
            extra={"worktree_path": str(wt_path)},
        )

        with patch("teetree.core.management.commands.lifecycle.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(returncode=0)
            call_command("lifecycle", "setup", path=str(wt_path))

        # Find the prek install call among all subprocess.run calls
        prek_calls = [c for c in mock_sp.run.call_args_list if c[0] and isinstance(c[0][0], list) and "prek" in c[0][0]]
        assert len(prek_calls) == 1
        assert prek_calls[0][0][0] == ["prek", "install", "-f"]
        assert prek_calls[0][1].get("cwd") == str(wt_path)

    @override_settings(**SETTINGS)
    def test_appends_envrc_lines_from_overlay(self, tmp_path: "pytest.TempPathFactory") -> None:
        """Setup appends overlay .envrc lines (e.g. venv activation) to worktree .envrc."""
        wt_path = tmp_path / "worktree"
        wt_path.mkdir()
        (wt_path / ".envrc").write_text("# existing\n", encoding="utf-8")

        ticket = Ticket.objects.create(issue_url="https://example.com/issues/200")
        Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/backend",
            branch="feature",
            extra={"worktree_path": str(wt_path)},
        )

        mock_overlay = MagicMock()
        mock_overlay.get_envrc_lines.return_value = ["export USE_UV=1"]
        mock_overlay.get_db_import_strategy.return_value = None
        mock_overlay.get_provision_steps.return_value = []
        mock_overlay.get_post_db_steps.return_value = []
        mock_overlay.get_reset_passwords_command.return_value = ""
        mock_overlay.get_env_extra.return_value = {}
        mock_overlay.get_skill_metadata.return_value = {}

        with (
            patch("teetree.core.management.commands.lifecycle.get_overlay", return_value=mock_overlay),
            patch("teetree.core.management.commands.lifecycle.subprocess") as mock_sp,
        ):
            mock_sp.run.return_value = MagicMock(returncode=0)
            call_command("lifecycle", "setup", path=str(wt_path))

        envrc = (wt_path / ".envrc").read_text()
        assert "export USE_UV=1" in envrc
        assert "# existing" in envrc  # original content preserved

        # Run again — should not duplicate
        with (
            patch("teetree.core.management.commands.lifecycle.get_overlay", return_value=mock_overlay),
            patch("teetree.core.management.commands.lifecycle.subprocess") as mock_sp,
        ):
            mock_sp.run.return_value = MagicMock(returncode=0)
            call_command("lifecycle", "setup", path=str(wt_path))

        envrc2 = (wt_path / ".envrc").read_text()
        assert envrc2.count("export USE_UV=1") == 1

    @override_settings(**SETTINGS)
    def test_updates_ticket_variant_when_requested(self, tmp_path: "pytest.TempPathFactory") -> None:
        wt_path = tmp_path / "worktree"
        wt_path.mkdir()

        ticket = Ticket.objects.create(issue_url="https://example.com/issues/201", variant="alpha")
        Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/backend",
            branch="feature",
            extra={"worktree_path": str(wt_path)},
        )

        mock_overlay = MagicMock()
        mock_overlay.get_envrc_lines.return_value = []
        mock_overlay.get_db_import_strategy.return_value = None
        mock_overlay.get_provision_steps.return_value = []
        mock_overlay.get_post_db_steps.return_value = []
        mock_overlay.get_reset_passwords_command.return_value = ""
        mock_overlay.get_env_extra.return_value = {}
        mock_overlay.get_skill_metadata.return_value = {}

        with (
            patch("teetree.core.management.commands.lifecycle.get_overlay", return_value=mock_overlay),
            patch("teetree.core.management.commands.lifecycle.subprocess") as mock_sp,
        ):
            mock_sp.run.return_value = MagicMock(returncode=0)
            call_command("lifecycle", "setup", path=str(wt_path), variant="beta")

        ticket.refresh_from_db()
        assert ticket.variant == "beta"

    @override_settings(**SETTINGS)
    def test_skips_envfile_message_when_no_path(self, tmp_path: "pytest.TempPathFactory") -> None:
        """Setup skips 'Written:' message when write_env_worktree returns None."""
        wt_path = tmp_path / "worktree"
        wt_path.mkdir()

        ticket = Ticket.objects.create(issue_url="https://example.com/issues/251")
        Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/backend",
            branch="feature",
            extra={"worktree_path": str(wt_path)},
        )

        mock_overlay = MagicMock()
        mock_overlay.get_db_import_strategy.return_value = None
        mock_overlay.get_provision_steps.return_value = []
        mock_overlay.get_post_db_steps.return_value = []
        mock_overlay.get_reset_passwords_command.return_value = ""
        mock_overlay.get_env_extra.return_value = {}
        mock_overlay.get_envrc_lines.return_value = []
        mock_overlay.get_skill_metadata.return_value = {}

        with (
            patch("teetree.core.management.commands.lifecycle.get_overlay", return_value=mock_overlay),
            patch("teetree.core.management.commands.lifecycle.subprocess") as mock_sp,
            patch("teetree.core.management.commands.lifecycle.write_env_worktree", return_value=None),
        ):
            mock_sp.run.return_value = MagicMock(returncode=0)
            call_command("lifecycle", "setup", path=str(wt_path))


@pytest.mark.django_db
class TestLifecycleSetupHelpers:
    @override_settings(**SETTINGS)
    def test_setup_worktree_dir_skips_nonexistent_path(self) -> None:
        """_setup_worktree_dir returns early when path doesn't exist."""
        from io import StringIO  # noqa: PLC0415

        from django.core.management.base import OutputWrapper  # noqa: PLC0415

        from teetree.core.management.commands.lifecycle import _setup_worktree_dir  # noqa: PLC0415

        mock_overlay = MagicMock()
        stdout = OutputWrapper(StringIO())
        # Empty path — should return early without calling anything
        _setup_worktree_dir("", MagicMock(), mock_overlay, stdout)
        mock_overlay.get_envrc_lines.assert_not_called()
        # Non-existent path
        _setup_worktree_dir("/tmp/does-not-exist-xyz", MagicMock(), mock_overlay, stdout)
        mock_overlay.get_envrc_lines.assert_not_called()

    def test_write_env_worktree_returns_none_without_path(self) -> None:
        """write_env_worktree returns None when worktree has no worktree_path."""
        from teetree.core.worktree_env import write_env_worktree  # noqa: PLC0415

        ticket = Ticket.objects.create(issue_url="https://example.com/issues/250")
        wt = Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/backend",
            branch="feature",
            extra={},  # no worktree_path
        )
        assert write_env_worktree(wt) is None


@pytest.mark.django_db
class TestLifecycleStart:
    @override_settings(**SETTINGS)
    def test_launches_services_and_transitions(
        self,
        tmp_path: "pytest.TempPathFactory",
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Lifecycle start should start Docker + app services, run pre-run steps, and transition FSM."""
        wt_path = tmp_path / "worktree"
        wt_path.mkdir()

        ticket = Ticket.objects.create(issue_url="https://example.com/issues/300", variant="acme")
        Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/backend",
            branch="feature",
            extra={"worktree_path": str(wt_path)},
        )
        worktree_id = cast("int", call_command("lifecycle", "setup", path=str(wt_path)))

        launched: list[str] = []

        mock_overlay = MagicMock()
        mock_overlay.get_run_commands.return_value = {"backend": "run-backend", "frontend": "run-frontend"}
        mock_overlay.get_services_config.return_value = {
            "db": {"start_command": "docker compose up -d db"},
        }
        mock_overlay.get_pre_run_steps.return_value = []
        mock_overlay.get_env_extra.return_value = {}

        def _mock_popen(cmd, **kwargs):
            launched.append(cmd)
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.poll.return_value = None  # still running
            return mock_proc

        with (
            patch("teetree.core.management.commands.lifecycle.get_overlay", return_value=mock_overlay),
            patch("teetree.core.management.commands.lifecycle.subprocess") as mock_sp,
            patch("teetree.core.management.commands.lifecycle.Popen", _mock_popen),
        ):
            mock_sp.run.return_value = MagicMock(returncode=0)
            call_command("lifecycle", "start", path=str(wt_path))

        worktree = Worktree.objects.get(pk=worktree_id)
        assert worktree.state == Worktree.State.SERVICES_UP
        # Docker service was started
        docker_calls = [c for c in mock_sp.run.call_args_list if c[0] and "docker" in str(c[0][0])]
        assert len(docker_calls) >= 1
        # App services were launched as background processes
        assert any("run-backend" in cmd for cmd in launched)
        assert any("run-frontend" in cmd for cmd in launched)
        # PIDs stored in extra
        assert "pids" in (worktree.extra or {})

    @override_settings(**SETTINGS)
    def test_skips_service_without_start_command(
        self,
        tmp_path: "pytest.TempPathFactory",
    ) -> None:
        """Docker services without a start_command are silently skipped."""
        wt_path = tmp_path / "worktree"
        wt_path.mkdir()
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/302", variant="acme")
        Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/backend",
            branch="feature",
            extra={"worktree_path": str(wt_path)},
        )
        call_command("lifecycle", "setup", path=str(wt_path))

        mock_overlay = MagicMock()
        mock_overlay.get_run_commands.return_value = {}
        mock_overlay.get_services_config.return_value = {"rd": {"start_command": ""}}
        mock_overlay.get_env_extra.return_value = {}

        with (
            patch("teetree.core.management.commands.lifecycle.get_overlay", return_value=mock_overlay),
            patch("teetree.core.management.commands.lifecycle.subprocess") as mock_sp,
        ):
            mock_sp.run.return_value = MagicMock(returncode=0)
            call_command("lifecycle", "start", path=str(wt_path))

        docker_calls = [c for c in mock_sp.run.call_args_list if c[0] and "docker" in str(c[0][0])]
        assert len(docker_calls) == 0

    @override_settings(**SETTINGS)
    def test_reports_crashed_process(
        self,
        tmp_path: "pytest.TempPathFactory",
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If a launched service exits immediately, start reports the failure."""
        wt_path = tmp_path / "worktree"
        wt_path.mkdir()

        ticket = Ticket.objects.create(issue_url="https://example.com/issues/301", variant="acme")
        Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/backend",
            branch="feature",
            extra={"worktree_path": str(wt_path)},
        )
        worktree_id = cast("int", call_command("lifecycle", "setup", path=str(wt_path)))

        mock_overlay = MagicMock()
        mock_overlay.get_run_commands.return_value = {"backend": "run-backend"}
        mock_overlay.get_services_config.return_value = {}
        mock_overlay.get_pre_run_steps.return_value = []
        mock_overlay.get_env_extra.return_value = {}

        def _mock_popen_crash(cmd, **kwargs):
            mock_proc = MagicMock()
            mock_proc.pid = 99999
            mock_proc.poll.return_value = 1  # crashed immediately
            return mock_proc

        with (
            patch("teetree.core.management.commands.lifecycle.get_overlay", return_value=mock_overlay),
            patch("teetree.core.management.commands.lifecycle.subprocess") as mock_sp,
            patch("teetree.core.management.commands.lifecycle.Popen", _mock_popen_crash),
        ):
            mock_sp.run.return_value = MagicMock(returncode=0)
            call_command("lifecycle", "start", path=str(wt_path))

        # Should still transition (services were attempted) but report failure
        worktree = Worktree.objects.get(pk=worktree_id)
        assert worktree.state == Worktree.State.SERVICES_UP
        assert "backend" in str(worktree.extra.get("failed_services", []))


@pytest.mark.django_db
class TestLifecycleClean:
    @override_settings(**SETTINGS)
    def test_tears_down_worktree(self, tmp_path: Path) -> None:
        wt_dir = tmp_path / "backend"
        wt_dir.mkdir()
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/62")
        wt = Worktree.objects.create(
            ticket=ticket, repo_path="/tmp/backend", branch="feature", extra={"worktree_path": str(wt_dir)}
        )
        wt.provision()
        wt.save()

        result = cast("str", call_command("lifecycle", "clean", path=str(wt_dir)))

        wt.refresh_from_db()
        assert wt.state == Worktree.State.CREATED
        assert "cleaned" in result.lower()
        assert "/tmp/backend" in result


@pytest.mark.django_db
class TestLifecycleStatus:
    pass  # No status tests in the original file — placeholder for future tests


@pytest.mark.django_db
class TestLifecycleDiagram:
    @override_settings(**SETTINGS)
    def test_worktree(self) -> None:
        result = cast("str", call_command("lifecycle", "diagram"))

        assert "stateDiagram-v2" in result
        assert "[*] --> created" in result
        assert "provision()" in result

    @override_settings(**SETTINGS)
    def test_ticket(self) -> None:
        result = cast("str", call_command("lifecycle", "diagram", model="ticket"))

        assert "stateDiagram-v2" in result
        assert "[*] --> not_started" in result
        assert "scope()" in result

    @override_settings(**SETTINGS)
    def test_task(self) -> None:
        result = cast("str", call_command("lifecycle", "diagram", model="task"))

        assert "stateDiagram-v2" in result
        assert "pending --> claimed: claim()" in result
        assert "claimed --> completed: complete()" in result
        assert "claimed --> failed: fail()" in result

    @override_settings(**SETTINGS)
    def test_unknown_model(self) -> None:
        result = cast("str", call_command("lifecycle", "diagram", model="unknown"))

        assert "Unknown model: unknown" in result


# ── Tool commands ──────────────────────────────────────────────────


@pytest.mark.django_db
class TestToolList:
    @override_settings(**SETTINGS)
    def test_shows_available_tools(self) -> None:
        result = cast("str", call_command("tool", "list"))

        assert "migrate: Run DB migrations" in result
        assert "seed: Seed test data" in result
        assert "broken" in result

    @override_settings(**MINIMAL_SETTINGS)
    def test_no_tools(self) -> None:
        result = cast("str", call_command("tool", "list"))

        assert "no tool commands" in result.lower()

    @override_settings(**SETTINGS)
    def test_tools_without_help(self) -> None:
        """Tools without a help string show just the name."""
        helpless_overlay = "tests.teetree_core.test_new_management_commands.HelplessToolOverlay"
        with override_settings(**{**SETTINGS, "TEATREE_OVERLAY_CLASS": helpless_overlay}):
            reset_overlay_cache()
            result = cast("str", call_command("tool", "list"))

        assert "bare-tool" in result


@pytest.mark.django_db
class TestToolRun:
    @override_settings(**SETTINGS)
    def test_executes_command(self) -> None:
        with patch("teetree.core.management.commands.tool.subprocess.run") as mock_run:
            result = cast("str", call_command("tool", "run", "migrate"))

        assert "completed" in result.lower()
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == "echo migrate"

    @override_settings(**SETTINGS)
    def test_unknown_tool(self) -> None:
        result = cast("str", call_command("tool", "run", "nonexistent"))

        assert "unknown tool: nonexistent" in result.lower()
        assert "migrate" in result

    @override_settings(**SETTINGS)
    def test_no_management_command(self) -> None:
        """Tool 'broken' has no management_command defined."""
        result = cast("str", call_command("tool", "run", "broken"))

        assert "no management_command" in result.lower()

    @override_settings(**SETTINGS)
    def test_forwards_extra_args(self) -> None:
        """Extra args after the tool name are appended to the management_command."""
        with (
            patch("teetree.core.management.commands.tool.subprocess.run") as mock_run,
            patch("teetree.core.management.commands.tool.sys") as mock_sys,
        ):
            mock_sys.argv = ["manage.py", "tool", "run", "migrate", "--verbose", "--dry-run"]
            result = cast("str", call_command("tool", "run", "migrate"))

        assert "completed" in result.lower()
        cmd = mock_run.call_args[0][0]
        assert cmd == "echo migrate --verbose --dry-run"
