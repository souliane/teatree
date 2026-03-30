from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import override_settings

from teatree.core.models import Session, Task, TaskAttempt, Ticket, Worktree
from teatree.core.overlay import OverlayBase, ProvisionStep, RunCommands
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
            "backend": f"run-backend {worktree.repo_path}",
            "frontend": f"run-frontend {worktree.repo_path}",
        }


@pytest.fixture(autouse=True)
def clear_overlay_cache() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()


_MOCK_OVERLAY = {"test": CommandOverlay()}

COMMAND_SETTINGS = {
    "TEATREE_HEADLESS_RUNTIME": "claude-code",
    "TEATREE_INTERACTIVE_RUNTIME": "codex",
    "TEATREE_TERMINAL_MODE": "same-terminal",
}


class TestLifecycleCommands:
    @override_settings(**COMMAND_SETTINGS)
    @pytest.mark.django_db
    def test_create_start_report_and_teardown_worktrees(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
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
        monkeypatch.setenv("T3_ORIG_CWD", wt_path)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            worktree_id = cast("int", call_command("lifecycle", "setup"))
            status = cast("dict[str, str]", call_command("lifecycle", "status"))
            call_command("lifecycle", "start")
            call_command("lifecycle", "teardown")

        worktree = Worktree.objects.get(pk=worktree_id)

        assert worktree_id == wt.id
        assert status["state"] == Worktree.State.PROVISIONED
        assert status["repo_path"] == "/tmp/backend"
        assert worktree.state == Worktree.State.CREATED
        assert worktree.extra == {}


class TestTaskCommands:
    @override_settings(**COMMAND_SETTINGS)
    @pytest.mark.django_db
    def test_claim_and_complete_work(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test", agent_id="agent-1")
        sdk_task = Task.objects.create(ticket=ticket, session=session)
        sdk_followup_task = Task.objects.create(ticket=ticket, session=session)
        user_task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )

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
        assert sdk_result["runtime"] == "claude-code"
        assert user_result["runtime"] == "codex"
        assert sdk_task.status == Task.Status.CLAIMED
        assert sdk_followup_task.status == Task.Status.COMPLETED
        assert user_task.status == Task.Status.COMPLETED
        assert TaskAttempt.objects.count() == 2
        assert refresh_summary == {"tickets": 1, "tasks": 3, "open_tasks": 1}
        assert reminders == []

    @override_settings(**COMMAND_SETTINGS)
    @pytest.mark.django_db
    def test_return_none_when_no_work_available(self) -> None:
        assert call_command("tasks", "work-next-sdk", claimed_by="worker-1") is None
        assert call_command("tasks", "work-next-user-input", claimed_by="worker-2") is None


class TestFollowupCommands:
    @pytest.mark.django_db
    def test_sync_reports_no_repos_from_default_overlay(self) -> None:
        with patch(
            "teatree.core.overlay_loader._discover_overlays",
            return_value=_MOCK_OVERLAY,
        ):
            result = cast("dict[str, int | list[str]]", call_command("followup", "sync"))

        assert result["errors"] == ["GitLab token is not configured in overlay"]
