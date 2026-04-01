from unittest.mock import patch

import pytest
from django.test import TestCase, override_settings

import teatree.core.overlay_loader as overlay_loader_mod
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.tasks import execute_headless_task, refresh_followup_snapshot, sync_followup
from tests.teatree_core.conftest import CommandOverlay

IMMEDIATE_BACKEND = {
    "TASKS": {
        "default": {
            "BACKEND": "django_tasks.backends.immediate.ImmediateBackend",
        },
    },
}

_MOCK_OVERLAY = {"test": CommandOverlay()}


class TestRefreshFollowupSnapshot(TestCase):
    @override_settings(**IMMEDIATE_BACKEND)
    def test_reports_current_counts(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        Task.objects.create(ticket=ticket, session=session)

        result = refresh_followup_snapshot.enqueue()

        assert result.return_value == {"tickets": 1, "tasks": 1, "open_tasks": 1}


class TestSyncFollowup(TestCase):
    @pytest.fixture(autouse=True)
    def _setup_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch

    @override_settings(**IMMEDIATE_BACKEND)
    def test_returns_error_without_token(self) -> None:
        with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY):
            result = sync_followup.enqueue()

        errors = result.return_value["errors"]
        assert len(errors) == 1
        assert "No code host token configured" in errors[0]


class TestExecuteHeadlessTask(TestCase):
    @pytest.fixture(autouse=True)
    def _setup_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch

    @override_settings(**IMMEDIATE_BACKEND)
    def test_records_failure_on_exception(self) -> None:
        """When run_headless raises, execute_headless_task marks the task as failed and re-raises."""
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test", agent_id="agent-1")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")

        def _raise(*_args: object, **_kwargs: object) -> None:
            msg = "headless runtime crashed"
            raise RuntimeError(msg)

        self.monkeypatch.setattr("teatree.agents.headless.run_headless", _raise)

        with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY):
            execute_headless_task.enqueue(int(task.pk), "coding")

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        attempt = TaskAttempt.objects.filter(task=task).first()
        assert attempt is not None
        assert attempt.exit_code == 1
        assert "headless runtime crashed" in attempt.error
