"""Regression tests for ``Task._apply_phase_transition`` (#1000).

The #998/#999 orphan sweep can mark a second reviewing task COMPLETED on a
reviewer-role ticket that already advanced to DELIVERED. Without a state
guard on the ``phase == "reviewing" and role == REVIEWER`` branch the FSM
raises ``TransitionNotAllowed`` and the loop tick crashes.
"""

import pytest
from django.test import TestCase
from django_fsm import TransitionNotAllowed

from teatree.core.models import Session, Task, Ticket


class TestApplyPhaseTransitionGuardsTerminalReviewer(TestCase):
    """#1000: reviewer-ticket already in DELIVERED must not re-fire the FSM."""

    def test_completed_reviewing_task_on_delivered_ticket_no_ops(self) -> None:
        # Reviewer-role ticket already advanced through review and is now
        # DELIVERED (terminal). The #999 orphan sweep then completes a
        # second reviewing task on the same ticket.
        ticket = Ticket.objects.create(
            overlay="test",
            role=Ticket.Role.REVIEWER,
            state=Ticket.State.DELIVERED,
        )
        session = Session.objects.create(ticket=ticket, agent_id="t")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            status=Task.Status.COMPLETED,
        )

        # Pre-#1000 this raised TransitionNotAllowed and crashed the tick.
        fired = task._apply_phase_transition()

        ticket.refresh_from_db()
        assert fired is False, "no transition should fire on a terminal-state reviewer ticket"
        assert ticket.state == Ticket.State.DELIVERED, f"ticket state must remain DELIVERED, got {ticket.state}"

    def test_reviewer_ticket_in_source_state_still_advances(self) -> None:
        # Guard must not regress the happy path: a reviewer-role ticket
        # still in a source state of mark_reviewed_externally() (here
        # NOT_STARTED, the lowest state on the source list) must advance
        # to DELIVERED when its reviewing task completes.
        ticket = Ticket.objects.create(
            overlay="test",
            role=Ticket.Role.REVIEWER,
            state=Ticket.State.NOT_STARTED,
        )
        session = Session.objects.create(ticket=ticket, agent_id="t")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            status=Task.Status.COMPLETED,
        )

        fired = task._apply_phase_transition()

        ticket.refresh_from_db()
        assert fired is True, "reviewer ticket in a source state must advance"
        assert ticket.state == Ticket.State.DELIVERED

    def test_mark_reviewed_externally_still_raises_when_called_directly_on_delivered(self) -> None:
        # Sanity: the guard lives in _apply_phase_transition, not in the
        # FSM. Calling the transition directly on a DELIVERED ticket
        # still raises — proving the guard is what protects the loop.
        ticket = Ticket.objects.create(
            overlay="test",
            role=Ticket.Role.REVIEWER,
            state=Ticket.State.DELIVERED,
        )
        session = Session.objects.create(ticket=ticket, agent_id="t")
        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            status=Task.Status.COMPLETED,
        )

        with pytest.raises(TransitionNotAllowed):
            ticket.mark_reviewed_externally()


class TestApplyPhaseTransitionChainsParentTask(TestCase):
    """PR-12: the completing task is threaded as ``parent_task`` of the next phase.

    A phase-boundary brief is warm only if the next-phase task chains back to
    the task that just finished — ``agents.prompt._parent_result_summary``
    reads ``task.parent_task``'s last attempt. Before this the FSM transition
    scheduled the next phase with ``parent_task=None`` (a cold brief).
    """

    def _author_ticket(self, state: str) -> Ticket:
        return Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR, state=state)

    def _completed_task(self, ticket: Ticket, phase: str) -> Task:
        session = Session.objects.create(ticket=ticket, agent_id=phase)
        return Task.objects.create(ticket=ticket, session=session, phase=phase, status=Task.Status.COMPLETED)

    def test_coding_completion_chains_testing_task_to_the_coder(self) -> None:
        ticket = self._author_ticket(Ticket.State.PLANNED)
        coding_task = self._completed_task(ticket, "coding")

        assert coding_task._apply_phase_transition() is True

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.CODED
        testing = Task.objects.get(ticket=ticket, phase="testing")
        assert testing.parent_task_id == coding_task.pk

    def test_testing_completion_chains_review_task_to_the_tester(self) -> None:
        ticket = self._author_ticket(Ticket.State.CODED)
        testing_task = self._completed_task(ticket, "testing")

        assert testing_task._apply_phase_transition() is True

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.TESTED
        review = Task.objects.get(ticket=ticket, phase="reviewing")
        assert review.parent_task_id == testing_task.pk
