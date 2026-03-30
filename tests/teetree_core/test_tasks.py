from collections.abc import Iterator

import pytest
from django.tasks import TaskResultStatus
from django.test import override_settings

from teetree.agents.services import RuntimeExecution, register_runtime, reset_runtime_registry
from teetree.core.models import Session, Task, TaskAttempt, Ticket
from teetree.core.tasks import execute_headless_task, execute_sdk_task, refresh_followup_snapshot, sync_followup


class TaskRuntime:
    def run(self, *, task: Task, skills: list[str], terminal_mode: str | None = None) -> RuntimeExecution:
        return RuntimeExecution(
            runtime="queued-sdk",
            artifact_path=f"artifacts/task-{task.pk}-queued-sdk.json",
            metadata={"skills": skills, "terminal_mode": terminal_mode},
        )


@pytest.fixture(autouse=True)
def reset_runtimes() -> Iterator[None]:
    reset_runtime_registry()
    yield
    reset_runtime_registry()


IMMEDIATE_BACKEND = {
    "TASKS": {
        "default": {
            "BACKEND": "django_tasks.backends.immediate.ImmediateBackend",
        },
    },
}


class TestExecuteSdkTask:
    @override_settings(**IMMEDIATE_BACKEND, TEATREE_HEADLESS_RUNTIME="queued-sdk")
    @pytest.mark.django_db
    def test_enqueue_runs_immediately(self) -> None:
        register_runtime("queued-sdk", TaskRuntime())
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=ticket, session=session)

        result = execute_sdk_task.enqueue(int(task.pk), "coding")
        task.refresh_from_db()

        assert result.status == TaskResultStatus.SUCCESSFUL
        assert result.return_value == f"artifacts/task-{task.pk}-queued-sdk.json"
        assert task.status == Task.Status.COMPLETED
        assert TaskAttempt.objects.count() == 1


class TestRefreshFollowupSnapshot:
    @override_settings(**IMMEDIATE_BACKEND)
    @pytest.mark.django_db
    def test_reports_current_counts(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        Task.objects.create(ticket=ticket, session=session)

        result = refresh_followup_snapshot.enqueue()

        assert result.return_value == {"tickets": 1, "tasks": 1, "open_tasks": 1}


class TestSyncFollowup:
    @override_settings(
        **IMMEDIATE_BACKEND,
        TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
        TEATREE_GITLAB_TOKEN="",
    )
    @pytest.mark.django_db
    def test_returns_error_without_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = sync_followup.enqueue()

        assert "TEATREE_GITLAB_TOKEN is not set" in result.return_value["errors"]


class TestExecuteHeadlessTask:
    @override_settings(
        **IMMEDIATE_BACKEND,
        TEATREE_OVERLAY_CLASS="tests.teetree_core.conftest.CommandOverlay",
    )
    @pytest.mark.django_db
    def test_records_failure_on_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When run_headless raises, execute_headless_task marks the task as failed and re-raises."""
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")

        def _raise(*_args: object, **_kwargs: object) -> None:
            msg = "headless runtime crashed"
            raise RuntimeError(msg)

        monkeypatch.setattr("teetree.agents.headless.run_headless", _raise)

        execute_headless_task.enqueue(int(task.pk), "coding")

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        attempt = TaskAttempt.objects.filter(task=task).first()
        assert attempt is not None
        assert attempt.exit_code == 1
        assert "headless runtime crashed" in attempt.error
