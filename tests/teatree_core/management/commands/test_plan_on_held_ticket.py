"""Recording a plan for a re-fix on a HELD PR.

A PR under a review HOLD leaves its ticket at ``pr_opened``/``review_requested``, and
``ticket.plan()``'s only FSM source is ``WORK_STARTED`` — so a planner that re-planned a
held PR produced its plan and had nowhere to put it (deferred question 488). Every
re-fix after a HOLD was then dispatched against a plan predating the review findings.

``_advance_with`` (#4409) decoupled the two halves: the artifact write is
unconditional and the WORK_STARTED → PLAN_RECORDED advance is attempted only when it can apply.
These pin the three properties the ticket's acceptance asks for — the plan SURVIVES,
no coding task is dispatched behind the hold, and the in-flight state is not
rewound — so a later widening of the FSM edge cannot quietly reintroduce any of them.
"""

from typing import cast

from django.test import TestCase

from teatree.core.management.commands._plan_gate_commands import record_artifact_and_advance
from teatree.core.models import Ticket
from teatree.core.models.plan_artifact import PlanArtifact
from tests.factories import TEST_ADEQUACY, TicketFactory

_HELD_STATES = [Ticket.State.PR_OPENED, Ticket.State.REVIEW_REQUESTED]
_FORTY_HEX = "c" * 40


def _record(state: str) -> "Ticket":
    ticket = cast("Ticket", TicketFactory(state=state))
    record_artifact_and_advance(
        ticket=ticket,
        plan_text="re-plan after the HOLD: the defect class is the fail-open probe, not the one line",
        recorded_by="planner",
        base_sha=_FORTY_HEX,
        adequacy=TEST_ADEQUACY,
    )
    return ticket


class PlanOnHeldTicket(TestCase):
    def test_the_plan_survives(self) -> None:
        for state in _HELD_STATES:
            with self.subTest(state=str(state)):
                ticket = _record(state)
                assert PlanArtifact.objects.filter(ticket=ticket).count() == 1

    def test_no_coding_task_is_dispatched_behind_the_hold(self) -> None:
        for state in _HELD_STATES:
            with self.subTest(state=str(state)):
                ticket = _record(state)
                assert not ticket.tasks.filter(phase="coding").exists()

    def test_the_in_flight_state_is_not_rewound(self) -> None:
        for state in _HELD_STATES:
            with self.subTest(state=str(state)):
                ticket = _record(state)
                ticket.refresh_from_db()
                assert ticket.state == state
