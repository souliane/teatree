"""Single-dispatch / liveness for author phase tasks.

With the loop dispatching per phase (``pending-spawn``/``claim-next`` →
the phase agent), the ``post_save`` auto-enqueue must NOT also drain a
loop-dispatched phase task through ``execute_headless_task`` — that would
run the same task twice. The loop is the SOLE dispatcher for author phase
tasks. Conversely, a task with no registered phase agent (and a genuinely
headless one like the self-improve executor) must STILL be auto-enqueued —
never zero dispatch.
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

_AUTHOR_PHASES = ("coding", "testing", "reviewing", "shipping")


class TestLoopDispatchedPhaseNotAutoEnqueued(TestCase):
    """An author phase task the loop dispatches is not also drained on creation."""

    def _author_ticket(self) -> Ticket:
        return Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR)

    @override_settings(**IMMEDIATE_BACKEND)
    def test_author_phase_task_creation_does_not_enqueue_execute_headless(self) -> None:
        ticket = self._author_ticket()
        session = Session.objects.create(ticket=ticket, agent_id="t")
        for phase in _AUTHOR_PHASES:
            with (
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
                patch("teatree.core.tasks.execute_headless_task") as headless,
            ):
                Task.objects.create(
                    ticket=ticket,
                    session=session,
                    phase=phase,
                    execution_target=Task.ExecutionTarget.HEADLESS,
                    status=Task.Status.PENDING,
                )
            assert headless.enqueue.call_count == 0, (
                f"author phase {phase!r} task was auto-enqueued for headless execution; "
                "the loop is the sole dispatcher for loop-dispatched phase tasks (double-dispatch)"
            )

    @override_settings(**IMMEDIATE_BACKEND)
    def test_reviewer_role_reviewing_task_not_auto_enqueued(self) -> None:
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.REVIEWER)
        session = Session.objects.create(ticket=ticket, agent_id="t")
        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.tasks.execute_headless_task") as headless,
        ):
            Task.objects.create(
                ticket=ticket,
                session=session,
                phase="reviewing",
                execution_target=Task.ExecutionTarget.HEADLESS,
                status=Task.Status.PENDING,
            )
        assert headless.enqueue.call_count == 0


class TestNonLoopDispatchedTaskStillAutoEnqueued(TestCase):
    """A headless task with no registered phase agent must still be drained.

    The double-dispatch guard must be surgical: it suppresses ONLY the
    loop-dispatched ``(role, phase)`` pairs. A genuinely headless task with
    no registered phase agent (a free-form phase) still rides the
    ``execute_headless_task`` path — never zero dispatch.
    """

    @override_settings(**IMMEDIATE_BACKEND)
    def test_unregistered_phase_task_is_auto_enqueued(self) -> None:
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR)
        session = Session.objects.create(ticket=ticket, agent_id="t")
        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.tasks.execute_headless_task") as headless,
        ):
            task = Task.objects.create(
                ticket=ticket,
                session=session,
                phase="architectural_review",
                execution_target=Task.ExecutionTarget.HEADLESS,
                status=Task.Status.PENDING,
            )
        headless.enqueue.assert_called_once_with(task.pk, "architectural_review")
