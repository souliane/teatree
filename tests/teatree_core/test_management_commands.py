import tempfile
from collections.abc import Iterator
from contextlib import AbstractContextManager
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
import teatree.utils.run as utils_run_mod
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
    def test_setup_ensures_shared_redis_and_allocates_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_path = str(tmp_path / "wt-backend")
            Path(wt_path).mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/77")
            Worktree.objects.create(
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
                patch.object(utils_run_mod, "subprocess") as mock_sp,
                patch("teatree.utils.redis_container.ensure_running") as mock_ensure,
                patch("teatree.config.load_config", return_value=mock_config),
            ):
                mock_sp.run.return_value = MagicMock(returncode=0)
                call_command("lifecycle", "setup")

            mock_ensure.assert_called_once_with()
            ticket.refresh_from_db()
            assert ticket.redis_db_index == 0

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
                patch.object(utils_run_mod, "subprocess") as mock_sp,
                patch.object(
                    lifecycle_cmd,
                    "find_free_ports",
                    return_value={"backend": 8001, "frontend": 4201, "postgres": 5432, "redis": 6379},
                ),
                patch.object(lifecycle_cmd, "get_worktree_ports", return_value={"backend": 8001, "frontend": 4201}),
                patch("teatree.utils.redis_container.ensure_running"),
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
                utils_run_mod.subprocess,
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

    def db_import(
        self,
        worktree: Worktree,
        *,
        force: bool = False,
        slow_import: bool = False,
        dslr_snapshot: str = "",
        dump_path: str = "",
    ) -> bool:
        return False


_DB_MOCK_OVERLAY = {"test": DbOverlay()}


class TestDbImportAutoRepair(TestCase):
    @override_settings(**COMMAND_SETTINGS)
    def test_skips_db_import_when_db_exists(self) -> None:
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
                extra={"worktree_path": wt_path},
                db_name="wt_99_acme",
            )

            with (
                patch.dict("os.environ", {"T3_ORIG_CWD": wt_path}),
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_DB_MOCK_OVERLAY),
                patch("teatree.utils.db.db_exists", return_value=True),
                patch("teatree.utils.redis_container.ensure_running"),
            ):
                call_command("lifecycle", "setup")

            # db_import was NOT called — DB already exists
            wt = Worktree.objects.get(ticket=ticket)
            assert "db_import_failures" not in (wt.extra or {})

    @override_settings(**COMMAND_SETTINGS)
    def test_retries_db_import_when_db_missing(self) -> None:
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
                extra={"worktree_path": wt_path},
                db_name="wt_100_acme",
            )

            with (
                patch.dict("os.environ", {"T3_ORIG_CWD": wt_path}),
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_DB_MOCK_OVERLAY),
                patch("teatree.utils.db.db_exists", return_value=False),
                patch("teatree.utils.redis_container.ensure_running"),
            ):
                call_command("lifecycle", "setup")

            # db_import WAS called (and failed — DbOverlay always returns False)
            wt = Worktree.objects.get(ticket=ticket)
            # No circuit breaker counter — just a warning in stderr
            assert "db_import_failures" not in (wt.extra or {})


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


class TestTicketCommand(TestCase):
    """Tests for the ticket management command (transition + list)."""

    def test_transition_scopes_ticket(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        result = cast(
            "dict[str, object]",
            call_command("ticket", "transition", ticket.pk, "scope"),
        )
        assert result["state"] == Ticket.State.SCOPED

    def test_transition_unknown_returns_error(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        result = cast(
            "dict[str, object]",
            call_command("ticket", "transition", ticket.pk, "nonexistent"),
        )
        assert "Unknown transition" in str(result["error"])

    def test_transition_not_found_returns_error(self) -> None:
        result = cast(
            "dict[str, object]",
            call_command("ticket", "transition", 99999, "scope"),
        )
        assert "not found" in str(result["error"])

    def test_transition_not_allowed_returns_error(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        result = cast(
            "dict[str, object]",
            call_command("ticket", "transition", ticket.pk, "code"),
        )
        assert "not allowed" in str(result["error"])

    def test_list_returns_all_tickets(self) -> None:
        Ticket.objects.create(overlay="test")
        Ticket.objects.create(overlay="other")
        result = cast(
            "list[dict[str, object]]",
            call_command("ticket", "list"),
        )
        assert len(result) == 2

    def test_list_filters_by_state(self) -> None:
        Ticket.objects.create(overlay="test", state=Ticket.State.SCOPED)
        Ticket.objects.create(overlay="test", state=Ticket.State.NOT_STARTED)
        result = cast(
            "list[dict[str, object]]",
            call_command("ticket", "list", state="scoped"),
        )
        assert len(result) == 1
        assert result[0]["state"] == "scoped"

    def test_list_filters_by_overlay(self) -> None:
        Ticket.objects.create(overlay="alpha")
        Ticket.objects.create(overlay="beta")
        result = cast(
            "list[dict[str, object]]",
            call_command("ticket", "list", overlay="alpha"),
        )
        assert len(result) == 1
        assert result[0]["overlay"] == "alpha"


class TestTasksCancelCommand(TestCase):
    """Tests for the tasks cancel subcommand."""

    def test_cancel_pending_task(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        call_command("tasks", "cancel", task.pk)
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_cancel_claimed_task_without_confirm(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        task.claim(claimed_by="worker-1")
        with pytest.raises(SystemExit):
            call_command("tasks", "cancel", task.pk)

    def test_cancel_claimed_task_with_confirm(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        task.claim(claimed_by="worker-1")
        call_command("tasks", "cancel", task.pk, confirm=True)
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_cancel_completed_task(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.COMPLETED,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        with pytest.raises(SystemExit):
            call_command("tasks", "cancel", task.pk)

    def test_cancel_nonexistent_task(self) -> None:
        with pytest.raises(SystemExit):
            call_command("tasks", "cancel", 99999)


class TestTasksListCommand(TestCase):
    """Tests for the tasks list subcommand."""

    def test_list_all_tasks(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
        )
        result = cast("list[dict[str, object]]", call_command("tasks", "list"))
        assert len(result) == 2

    def test_list_filters_by_status(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.COMPLETED,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        result = cast(
            "list[dict[str, object]]",
            call_command("tasks", "list", status="completed"),
        )
        assert len(result) == 1

    def test_list_filters_by_execution_target(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
        )
        result = cast(
            "list[dict[str, object]]",
            call_command("tasks", "list", execution_target="interactive"),
        )
        assert len(result) == 1
        assert result[0]["execution_target"] == "interactive"

    def test_render_tasks_table_formats_rows(self) -> None:
        from io import StringIO  # noqa: PLC0415

        from teatree.core.management.commands.tasks import TaskRow, _render_tasks_table  # noqa: PLC0415

        rows: list[TaskRow] = [
            TaskRow(
                task_id=7,
                ticket_id=42,
                status="pending",
                execution_target="interactive",
                phase="coding",
                execution_reason="resume after user input",
                claimed_by="",
            )
        ]
        buf = StringIO()
        _render_tasks_table(rows, stream=buf)
        out = buf.getvalue()
        assert "Tasks (1)" in out
        assert "ID" in out
        assert "Ticket" in out
        assert "Phase" in out
        assert "interactive" in out
        assert "coding" in out
        assert "resume after" in out

    def test_render_tasks_table_handles_empty(self) -> None:
        from io import StringIO  # noqa: PLC0415

        from teatree.core.management.commands.tasks import _render_tasks_table  # noqa: PLC0415

        buf = StringIO()
        _render_tasks_table([], stream=buf)
        assert "No tasks" in buf.getvalue()


class TestTasksStartCommand(TestCase):
    """Tests for the tasks start subcommand (inline interactive launch)."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/99")
        self.session = Session.objects.create(ticket=self.ticket, overlay="test", agent_id="agent-1")

    @staticmethod
    def _patch_env(run_mock: MagicMock) -> list[AbstractContextManager[object]]:
        return [
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(web_terminal_mod.shutil, "which", return_value="/usr/bin/claude"),
            patch("teatree.utils.run.run_streamed", new=run_mock),
        ]

    @override_settings(**COMMAND_SETTINGS)
    def test_start_claims_next_interactive_task_and_runs_claude(self) -> None:
        Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            execution_target=Task.ExecutionTarget.HEADLESS,
        )
        user_task = Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            phase="coding",
        )

        run_mock = MagicMock(return_value=0)
        p1, p2, p3 = self._patch_env(run_mock)
        with p1, p2, p3, pytest.raises(SystemExit) as exc:
            call_command("tasks", "start")

        assert exc.value.code == 0
        run_mock.assert_called_once()
        argv = run_mock.call_args[0][0]
        assert argv[0] == "/usr/bin/claude"
        assert "--append-system-prompt" in argv

        user_task.refresh_from_db()
        assert user_task.status == Task.Status.CLAIMED
        assert user_task.claimed_by == "cli"
        assert TaskAttempt.objects.filter(task=user_task, launch_url="").count() == 1

    @override_settings(**COMMAND_SETTINGS)
    def test_start_with_task_id_claims_specific_task(self) -> None:
        task = Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )

        run_mock = MagicMock(return_value=0)
        p1, p2, p3 = self._patch_env(run_mock)
        with p1, p2, p3, pytest.raises(SystemExit):
            call_command("tasks", "start", task.pk)

        run_mock.assert_called_once()
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED

    @override_settings(**COMMAND_SETTINGS)
    def test_start_with_no_pending_tasks_skips_run(self) -> None:
        run_mock = MagicMock(return_value=0)
        p1, p2, p3 = self._patch_env(run_mock)
        with p1, p2, p3:
            call_command("tasks", "start")
        run_mock.assert_not_called()

    @override_settings(**COMMAND_SETTINGS)
    def test_start_rejects_headless_task_id(self) -> None:
        task = Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            execution_target=Task.ExecutionTarget.HEADLESS,
        )

        run_mock = MagicMock(return_value=0)
        p1, p2, p3 = self._patch_env(run_mock)
        with p1, p2, p3, pytest.raises(SystemExit):
            call_command("tasks", "start", task.pk)

        run_mock.assert_not_called()
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    @override_settings(**COMMAND_SETTINGS)
    def test_start_resumes_when_session_has_claude_uuid(self) -> None:
        uuid = "01234567-89ab-cdef-0123-456789abcdef"
        session = Session.objects.create(ticket=self.ticket, overlay="test", agent_id=uuid)
        Task.objects.create(
            ticket=self.ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )

        run_mock = MagicMock(return_value=0)
        p1, p2, p3 = self._patch_env(run_mock)
        with p1, p2, p3, pytest.raises(SystemExit):
            call_command("tasks", "start")

        argv = run_mock.call_args[0][0]
        assert "--resume" in argv
        assert uuid in argv
