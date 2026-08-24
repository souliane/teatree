"""``Ticket.begin_planning`` — the ladder walk that makes a planning task consumable (#4578).

Scheduling planning is not enough on its own: ``Ticket.plan``'s FSM source is exclusively
STARTED and ``Task._apply_phase_transition``'s planning branch is guarded on the same
state, so a planning task minted on a NOT_STARTED ticket completes into nothing. These
tests pin the walk, its idempotence, and its refusals.
"""

import pytest
from django.test import TestCase

from teatree.core.models import Session, Task, Ticket
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.plan_artifact import PlanArtifact


def _author(state: str = Ticket.State.NOT_STARTED) -> Ticket:
    return Ticket.objects.create(role=Ticket.Role.AUTHOR, state=state, overlay="acme")


class TestTheLadderWalk(TestCase):
    def test_a_not_started_ticket_lands_started_with_a_planning_task(self) -> None:
        ticket = _author()

        task = ticket.begin_planning()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED
        assert task.phase == "planning"
        assert task.ticket_id == ticket.pk

    def test_a_scoped_ticket_converges_on_started(self) -> None:
        ticket = _author(Ticket.State.SCOPED)

        ticket.begin_planning()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED

    def test_an_already_started_ticket_stays_started(self) -> None:
        ticket = _author(Ticket.State.STARTED)

        ticket.begin_planning()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED

    def test_the_planning_completion_now_advances_the_ticket_to_coding(self) -> None:
        """The whole point of the walk: the ladder must actually continue past planning."""
        ticket = _author()
        task = ticket.begin_planning()
        PlanArtifact.record(ticket=ticket, plan_text="the plan", recorded_by="t3:planner")

        task.complete()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.PLANNED
        assert Task.objects.filter(ticket=ticket, phase="coding").exists()


class TestIdempotenceAndRefusals(TestCase):
    def test_a_second_call_returns_the_in_flight_task(self) -> None:
        ticket = _author()
        first = ticket.begin_planning()

        assert ticket.begin_planning().pk == first.pk
        assert Task.objects.filter(ticket=ticket, phase="planning").count() == 1

    def test_a_ticket_past_the_early_states_is_refused(self) -> None:
        ticket = _author(Ticket.State.PLANNED)

        with pytest.raises(InvalidTransitionError):
            ticket.begin_planning()

    def test_a_reviewer_ticket_is_refused_and_keeps_its_state(self) -> None:
        """The walk and the mint share one ``atomic``, so a refusal leaves no half-advanced row."""
        ticket = Ticket.objects.create(role=Ticket.Role.REVIEWER, state=Ticket.State.NOT_STARTED, overlay="acme")

        with pytest.raises(InvalidTransitionError):
            ticket.begin_planning()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.NOT_STARTED
        assert not Task.objects.filter(ticket=ticket).exists()

    def test_a_parent_task_is_threaded_onto_the_scheduled_task(self) -> None:
        parent_ticket = _author(Ticket.State.STARTED)
        session = Session.objects.create(ticket=parent_ticket, agent_id="planning")
        parent = Task.objects.create(ticket=parent_ticket, session=session, phase="planning", subject="parent")
        ticket = _author()

        assert ticket.begin_planning(parent_task=parent).parent_task_id == parent.pk
