"""`tasks complete` must not wedge a task when the FSM advance is refused (#1977).

Live failure: ``tasks complete`` on a planning-phase task whose ticket has no
PlanArtifact drove ``Task.complete()`` → ``_advance_ticket`` → ``ticket.plan()``
→ ``NoPlanArtifactError``, which rolled back the WHOLE atomic completion and
exited rc=1 — the task stayed ``claimed`` forever (also unrescuable by the
reaper, which only rescues CLAIMED-with-expired-lease, not a freshly-completed
phantom). A deliberate gate refusal is not a crash: the task completion (the
operator's out-of-band-done bookkeeping) must persist, and the failed FSM
advance must be surfaced LOUDLY, not silently — beats a wedged claimed task.

The #883 atomicity invariant (task COMPLETED + ticket advance land together or
neither) still holds for a real crash; a TYPED transition refusal is the one
expected non-crash case where the task completes and the ticket simply does not
advance (the replay sweep fires the transition later once the artifact exists).
"""

import io

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Session, Task, Ticket

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


class TasksCompleteFsmAdvanceFailureTest(TestCase):
    def _claimed_planning_task_no_artifact(self) -> Task:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="planning",
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        task.claim(claimed_by="worker-1")
        return task

    def test_complete_does_not_wedge_task_when_fsm_advance_refused(self) -> None:
        task = self._claimed_planning_task_no_artifact()
        stderr = io.StringIO()

        # No SystemExit: the completion must succeed even though plan() refuses.
        call_command("tasks", "complete", task.pk, stderr=stderr)

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED  # task is NOT wedged claimed

        task.ticket.refresh_from_db()
        assert task.ticket.state == Ticket.State.STARTED  # ticket did NOT advance

        # The refusal is surfaced LOUDLY, not swallowed.
        out = stderr.getvalue().lower()
        assert "planartifact" in out or "plan" in out

    def test_complete_still_advances_ticket_when_artifact_present(self) -> None:
        # Regression guard: the happy planning path still advances the ticket.
        from teatree.core.models.plan_artifact import PlanArtifact  # noqa: PLC0415

        task = self._claimed_planning_task_no_artifact()
        PlanArtifact.record(ticket=task.ticket, plan_text="real plan", recorded_by="t3:planner")

        call_command("tasks", "complete", task.pk)

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        task.ticket.refresh_from_db()
        assert task.ticket.state == Ticket.State.PLANNED


class CompleteSurfacingAdvanceFailureModelTest(TestCase):
    """The model method itself: completion persists, refusal reason returned."""

    def _claimed_planning_task(self) -> Task:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, overlay="test")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="planning",
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )
        task.claim(claimed_by="worker-1")
        return task

    def test_returns_refusal_reason_and_keeps_task_completed(self) -> None:
        task = self._claimed_planning_task()
        reason = task.complete_surfacing_advance_failure()
        assert reason  # non-empty refusal reason returned
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        task.ticket.refresh_from_db()
        assert task.ticket.state == Ticket.State.STARTED

    def test_empty_message_refusal_falls_back_to_class_name(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.core.models.errors import InvalidTransitionError  # noqa: PLC0415

        task = self._claimed_planning_task()
        with patch.object(Task, "_advance_ticket", side_effect=InvalidTransitionError("")):
            reason = task.complete_surfacing_advance_failure()
        assert reason == "InvalidTransitionError"
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_returns_empty_string_on_clean_advance(self) -> None:
        from teatree.core.models.plan_artifact import PlanArtifact  # noqa: PLC0415

        task = self._claimed_planning_task()
        PlanArtifact.record(ticket=task.ticket, plan_text="p", recorded_by="t3:planner")
        reason = task.complete_surfacing_advance_failure()
        assert reason == ""
        task.ticket.refresh_from_db()
        assert task.ticket.state == Ticket.State.PLANNED
