from unittest.mock import patch

from django.test import TestCase, override_settings

import teatree.core.overlay_loader as overlay_loader_mod
import teatree.core.signals as signals_mod
import teatree.utils.run as utils_run_mod
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
                utils_run_mod.subprocess,
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
                utils_run_mod.subprocess,
                "run",
                return_value=_sp.CompletedProcess([], 0, '{"summary": "OK"}', ""),
            ),
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
        ):
            task.route_to_headless(reason="Auto-rerouted for testing")

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED


class TestSlackReactionsOnTransition(TestCase):
    """post_transition signal triggers Slack reactions via the overlay config."""

    def _ticket_with_mr(self) -> Ticket:
        return Ticket.objects.create(
            overlay="test",
            state=Ticket.State.IN_REVIEW,
            extra={
                "mrs": {
                    "https://gitlab.com/org/repo/-/merge_requests/1": {
                        "review_permalink": "https://team.slack.com/archives/C999/p1700000000000100",
                    }
                }
            },
        )

    def test_mark_merged_invokes_reactions(self) -> None:
        ticket = self._ticket_with_mr()
        called: list[tuple[object, str]] = []

        def _fake(t: object, name: str) -> int:
            called.append((t, name))
            return 1

        with patch.object(signals_mod, "add_reactions_for_transition", _fake):
            ticket.mark_merged()
            ticket.save()

        assert len(called) == 1
        assert called[0][1] == "mark_merged"

    def test_transition_survives_reaction_failure(self) -> None:
        ticket = self._ticket_with_mr()

        def _boom(*_a: object, **_kw: object) -> int:
            msg = "slack down"
            raise RuntimeError(msg)

        with patch.object(signals_mod, "add_reactions_for_transition", _boom):
            ticket.mark_merged()
            ticket.save()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED

    def test_different_transitions_forward_their_name(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED, extra={"mrs": {}})
        names: list[str] = []

        def _record(_ticket: object, name: str) -> int:
            names.append(name)
            return 0

        with patch.object(signals_mod, "add_reactions_for_transition", _record):
            ticket.rework()
            ticket.save()

        assert names == ["rework"]

    def test_ticket_without_mrs_is_noop(self) -> None:
        """The real handler is a silent no-op when the ticket has no MRs."""
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IN_REVIEW, extra={})
        # No patching — the real code path must not raise.
        ticket.mark_merged()
        ticket.save()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
