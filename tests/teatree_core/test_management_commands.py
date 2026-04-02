import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase, override_settings

import teatree.agents.headless as headless_mod
import teatree.agents.web_terminal as web_terminal_mod
import teatree.core.management.commands.lifecycle as lifecycle_cmd
import teatree.core.overlay_loader as overlay_loader_mod
from teatree.core.models import Session, Task, TaskAttempt, Ticket, Worktree
from teatree.core.overlay import DbImportStrategy, OverlayBase, ProvisionStep, RunCommands
from teatree.core.overlay_loader import reset_overlay_cache

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


class CommandOverlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        def remember_setup() -> None:
            facts = cast("dict[str, str]", worktree.extra or {})
            facts["setup_hook"] = "ran"
            worktree.extra = facts
            worktree.save(update_fields=["extra"])

        return [ProvisionStep(name="remember-setup", callable=remember_setup)]

    def get_run_commands(self, worktree: Worktree) -> RunCommands:
        return {
            "backend": ["run-backend", worktree.repo_path],
            "frontend": ["run-frontend", worktree.repo_path],
        }


@pytest.fixture(autouse=True)
def clear_overlay_cache() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()


_MOCK_OVERLAY = {"test": CommandOverlay()}

COMMAND_SETTINGS = {
    "TEATREE_TERMINAL_MODE": "same-terminal",
}


class TestLifecycleCommands(TestCase):
    @override_settings(**COMMAND_SETTINGS)
    def test_create_start_report_and_teardown_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_path = str(tmp_path / "test-worktree-backend")
            Path(wt_path).mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/55", variant="acme")
            wt = Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": wt_path},
            )

            mock_config = MagicMock()
            mock_config.user.workspace_dir = tmp_path

            with (
                patch.dict("os.environ", {"T3_ORIG_CWD": wt_path}),
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
                patch.object(lifecycle_cmd, "subprocess") as mock_sp,
                patch.object(
                    lifecycle_cmd,
                    "find_free_ports",
                    return_value={"backend": 8001, "frontend": 4201, "postgres": 5432, "redis": 6379},
                ),
                patch.object(lifecycle_cmd, "get_worktree_ports", return_value={"backend": 8001, "frontend": 4201}),
                patch("teatree.config.load_config", return_value=mock_config),
            ):
                mock_sp.run.return_value = MagicMock(returncode=0)
                worktree_id = cast("int", call_command("lifecycle", "setup"))
                status = cast("dict[str, str]", call_command("lifecycle", "status"))
                # start returns "error" since CommandOverlay has no compose file;
                # state stays PROVISIONED, which is fine for teardown
                call_command("lifecycle", "start")
                call_command("lifecycle", "teardown")

            worktree = Worktree.objects.get(pk=worktree_id)

            assert worktree_id == wt.id
            assert status["state"] == Worktree.State.PROVISIONED
            assert status["repo_path"] == "/tmp/backend"
            assert worktree.state == Worktree.State.CREATED
            assert worktree.extra == {}


class TestTaskCommands(TestCase):
    @override_settings(**COMMAND_SETTINGS)
    def test_claim_and_complete_work(self) -> None:
        import subprocess as _sp  # noqa: PLC0415

        from teatree.agents.terminal_launcher import LaunchResult  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test", agent_id="agent-1")
        sdk_task = Task.objects.create(ticket=ticket, session=session)
        sdk_followup_task = Task.objects.create(ticket=ticket, session=session)
        user_task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )

        mock_result = LaunchResult(launch_url="http://127.0.0.1:9999", pid=1, mode="ttyd")

        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(
                headless_mod.subprocess,
                "run",
                return_value=_sp.CompletedProcess(
                    [],
                    0,
                    '{"session_id": "test-session", "result": "```json\\n{\\"summary\\": \\"done\\"}\\n```"}',
                    "",
                ),
            ),
            patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(web_terminal_mod.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(web_terminal_mod, "terminal_launch", return_value=mock_result),
        ):
            claimed_task_id = cast(
                "int", call_command("tasks", "claim", execution_target="headless", claimed_by="worker-1")
            )
            sdk_result = cast(
                "dict[str, str]",
                call_command("tasks", "work-next-sdk", claimed_by="worker-1"),
            )
            user_result = cast(
                "dict[str, str]",
                call_command("tasks", "work-next-user-input", claimed_by="worker-2"),
            )
            refresh_summary = cast("dict[str, int]", call_command("followup", "refresh"))
            reminders = cast("list[int]", call_command("followup", "remind"))

        sdk_task.refresh_from_db()
        sdk_followup_task.refresh_from_db()
        user_task.refresh_from_db()

        assert claimed_task_id == sdk_task.id
        assert "exit_code" in sdk_result
        assert "launch_url" in user_result
        assert sdk_task.status == Task.Status.CLAIMED
        assert sdk_followup_task.status == Task.Status.COMPLETED
        assert user_task.status == Task.Status.CLAIMED
        assert TaskAttempt.objects.count() == 2
        assert refresh_summary == {"tickets": 1, "tasks": 3, "open_tasks": 2}
        assert reminders == []

    @override_settings(**COMMAND_SETTINGS)
    def test_return_none_when_no_work_available(self) -> None:
        assert call_command("tasks", "work-next-sdk", claimed_by="worker-1") is None
        assert call_command("tasks", "work-next-user-input", claimed_by="worker-2") is None


class DbOverlay(CommandOverlay):
    """CommandOverlay with a DB import strategy that always fails."""

    def get_db_import_strategy(self, worktree: Worktree) -> DbImportStrategy | None:
        return DbImportStrategy(kind="dslr")

    def db_import(self, worktree: Worktree, *, force: bool = False) -> bool:
        return False


_DB_MOCK_OVERLAY = {"test": DbOverlay()}


class TestDbImportCircuitBreaker(TestCase):
    @override_settings(**COMMAND_SETTINGS)
    def test_skips_db_import_after_max_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_path = str(tmp_path / "backend")
            Path(wt_path).mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/99")
            Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": wt_path, "db_import_failures": 3},
            )

            with (
                patch.dict("os.environ", {"T3_ORIG_CWD": wt_path}),
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_DB_MOCK_OVERLAY),
            ):
                call_command("lifecycle", "setup")

            # db_import was NOT called — failure count unchanged
            wt = Worktree.objects.get(ticket=ticket)
            assert wt.extra["db_import_failures"] == 3

    @override_settings(**COMMAND_SETTINGS)
    def test_force_bypasses_circuit_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_path = str(tmp_path / "backend")
            Path(wt_path).mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/100")
            Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": wt_path, "db_import_failures": 3},
            )

            with (
                patch.dict("os.environ", {"T3_ORIG_CWD": wt_path}),
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_DB_MOCK_OVERLAY),
            ):
                call_command("lifecycle", "setup", "--force")

            # db_import WAS called and failed again — count incremented
            wt = Worktree.objects.get(ticket=ticket)
            assert wt.extra["db_import_failures"] == 4


class TestUpdateTicketVariant(TestCase):
    def test_updates_ticket_variant_and_recomputes_db_name(self) -> None:
        from teatree.core.management.commands.lifecycle import _update_ticket_variant  # noqa: PLC0415

        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/200",
            variant="old",
        )
        wt = Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="backend",
            branch="feature",
            db_name=f"wt_{ticket.ticket_number}_old",
        )

        _update_ticket_variant(ticket, "new")

        ticket.refresh_from_db()
        wt.refresh_from_db()
        assert ticket.variant == "new"
        assert wt.db_name == f"wt_{ticket.ticket_number}_new"

    def test_skips_save_when_db_name_unchanged(self) -> None:
        from teatree.core.management.commands.lifecycle import _update_ticket_variant  # noqa: PLC0415

        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/201",
            variant="",
        )
        wt = Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="backend",
            branch="feature",
            db_name=f"wt_{ticket.ticket_number}",
        )
        original_db_name = wt.db_name

        # Variant "" → "acme" should change the db_name
        _update_ticket_variant(ticket, "acme")

        wt.refresh_from_db()
        assert wt.db_name != original_db_name
        assert wt.db_name == f"wt_{ticket.ticket_number}_acme"


class TestFollowupCommands(TestCase):
    def test_sync_reports_no_repos_from_default_overlay(self) -> None:
        with patch.object(
            overlay_loader_mod,
            "_discover_overlays",
            return_value=_MOCK_OVERLAY,
        ):
            result = cast("dict[str, int | list[str]]", call_command("followup", "sync"))

        errors = result["errors"]
        assert isinstance(errors, list)
        assert len(errors) == 1
        assert "No code host token for" in errors[0]
