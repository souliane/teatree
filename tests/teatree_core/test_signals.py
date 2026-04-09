from unittest.mock import patch

from django.test import TestCase, override_settings

import teatree.core.overlay_loader as overlay_loader_mod
from teatree.core.models import Session, Task, Ticket
from tests.teatree_core.conftest import CommandOverlay

IMMEDIATE_BACKEND = {
    "TASKS": {
        "default": {
            "BACKEND": "django_tasks.backends.immediate.ImmediateBackend",
        },
    },
}

_MOCK_OVERLAY = {"test": CommandOverlay()}


class TestAutoEnqueueHeadlessSignal(TestCase):
    """post_save signal auto-enqueues headless tasks on creation."""

    @override_settings(**IMMEDIATE_BACKEND)
    def test_headless_task_auto_executes_on_creation(self) -> None:
        import subprocess as _sp  # noqa: PLC0415

        import teatree.agents.headless as headless_mod  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")

        with (
            patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude-code"),
            patch.object(
                headless_mod.subprocess,
                "run",
                return_value=_sp.CompletedProcess([], 0, '{"summary": "OK"}', ""),
            ),
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
        ):
            task = Task.objects.create(
                ticket=ticket,
                session=session,
                execution_target=Task.ExecutionTarget.HEADLESS,
                phase="coding",
            )

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_interactive_task_not_enqueued(self) -> None:
        """Interactive tasks are not auto-enqueued by the signal."""
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")

        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            phase="coding",
        )

        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_non_pending_headless_task_not_enqueued(self) -> None:
        """Already-completed headless tasks are not re-enqueued."""
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")

        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.COMPLETED,
        )

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_signal_failure_leaves_task_pending(self) -> None:
        """If enqueue fails, the task remains PENDING for drain to retry."""
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")

        import teatree.core.tasks as tasks_mod  # noqa: PLC0415

        class BrokenEnqueue:
            @staticmethod
            def enqueue(*_args: object, **_kwargs: object) -> None:
                msg = "backend unavailable"
                raise RuntimeError(msg)

        with patch.object(tasks_mod, "execute_headless_task", BrokenEnqueue):
            task = Task.objects.create(
                ticket=ticket,
                session=session,
                execution_target=Task.ExecutionTarget.HEADLESS,
                phase="coding",
            )

        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    @override_settings(**IMMEDIATE_BACKEND)
    def test_route_to_headless_triggers_enqueue(self) -> None:
        """Re-routing an interactive task to headless triggers auto-enqueue."""
        import subprocess as _sp  # noqa: PLC0415

        import teatree.agents.headless as headless_mod  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test")

        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            phase="coding",
        )
        assert task.status == Task.Status.PENDING

        with (
            patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude-code"),
            patch.object(
                headless_mod.subprocess,
                "run",
                return_value=_sp.CompletedProcess([], 0, '{"summary": "OK"}', ""),
            ),
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
        ):
            task.route_to_headless(reason="Auto-rerouted for testing")

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
