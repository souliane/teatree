from unittest.mock import patch

import pytest
from django.test import TestCase, override_settings

import teatree.core.overlay_loader as overlay_loader_mod
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.tasks import (
    drain_headless_queue,
    execute_retrospect,
    refresh_followup_snapshot,
    sync_followup,
)
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
        assert "No code host token for" in errors[0]


class TestDrainHeadlessQueue(TestCase):
    """Drain is a safety net for tasks that missed the post_save auto-enqueue."""

    def setUp(self) -> None:
        from django.db.models.signals import post_save  # noqa: PLC0415

        from teatree.core.signals import _auto_enqueue_headless_task  # noqa: PLC0415

        post_save.disconnect(_auto_enqueue_headless_task, sender=Task, dispatch_uid="auto_enqueue_headless")
        self.addCleanup(
            post_save.connect, _auto_enqueue_headless_task, sender=Task, dispatch_uid="auto_enqueue_headless"
        )

    @override_settings(**IMMEDIATE_BACKEND)
    def test_enqueues_pending_headless_tasks(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")
        pending = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.PENDING,
            phase="coding",
        )
        # Interactive task should NOT be enqueued
        Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            status=Task.Status.PENDING,
            phase="testing",
        )

        with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY):
            result = drain_headless_queue.enqueue()

        assert result.return_value == {"enqueued": [pending.pk]}

    @override_settings(**IMMEDIATE_BACKEND)
    def test_skips_when_no_pending_tasks(self) -> None:
        with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY):
            result = drain_headless_queue.enqueue()

        assert result.return_value == {"enqueued": []}


class TestExecuteRetrospect(TestCase):
    @staticmethod
    def _ticket_in_merged() -> Ticket:
        ticket = Ticket.objects.create(overlay="test")
        ticket.state = Ticket.State.MERGED
        ticket.save(update_fields=["state"])
        return ticket

    @override_settings(**IMMEDIATE_BACKEND)
    def test_advances_merged_ticket_to_delivered(self) -> None:
        ticket = self._ticket_in_merged()
        ticket.state = Ticket.State.RETROSPECTED
        ticket.save(update_fields=["state"])

        result = execute_retrospect.enqueue(ticket.pk)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.DELIVERED
        assert ticket.extra.get("retro_scheduled") is True
        assert result.return_value == {"ticket_id": ticket.pk, "ok": True, "detail": "retro-scheduled"}

    @override_settings(**IMMEDIATE_BACKEND)
    def test_skips_when_state_does_not_match(self) -> None:
        """At-least-once delivery: redelivered jobs must be no-ops."""
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.DELIVERED)

        result = execute_retrospect.enqueue(ticket.pk)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.DELIVERED
        assert result.return_value == {
            "ticket_id": ticket.pk,
            "skipped": True,
            "state": "delivered",
        }


class TestExecuteHeadlessTask(TestCase):
    @pytest.fixture(autouse=True)
    def _setup_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch

    @override_settings(**IMMEDIATE_BACKEND)
    def test_records_failure_on_exception(self) -> None:
        """When run_headless raises, execute_headless_task marks the task as failed."""
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test", agent_id="agent-1")

        def _raise(*_args: object, **_kwargs: object) -> None:
            msg = "headless runtime crashed"
            raise RuntimeError(msg)

        self.monkeypatch.setattr("teatree.agents.headless.run_headless", _raise)

        # Signal auto-enqueues on creation; ImmediateBackend runs it synchronously
        with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY):
            task = Task.objects.create(ticket=ticket, session=session, phase="coding")

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        attempt = TaskAttempt.objects.filter(task=task).first()
        assert attempt is not None
        assert attempt.exit_code == 1
        assert "headless runtime crashed" in attempt.error
