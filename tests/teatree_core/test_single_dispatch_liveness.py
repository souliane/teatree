"""Liveness: every PENDING task reaches a dispatcher — never zero dispatch.

The ``post_save`` auto-enqueue owns a freshly PENDING task whether or not its
``(role, phase)`` has a registered phase agent, and the atomic claim CAS is what
keeps a second claimer from running the same row twice.
"""

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


class TestNonLoopDispatchedTaskStillAutoEnqueued(TestCase):
    """A task with no registered phase agent must still be drained."""

    @override_settings(**IMMEDIATE_BACKEND)
    def test_unregistered_phase_task_is_auto_enqueued(self) -> None:
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR)
        session = Session.objects.create(ticket=ticket, agent_id="t")
        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.tasks.execute_task") as headless,
        ):
            task = Task.objects.create(
                ticket=ticket,
                session=session,
                phase="architectural_review",
                status=Task.Status.PENDING,
            )
        headless.enqueue.assert_called_once_with(task.pk, "architectural_review")
