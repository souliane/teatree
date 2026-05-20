"""Regression tests for shipping-gate enforcement on task-based completion (#1284).

Codex finding #1282-2 (catastrophic): ``Task._apply_phase_transition`` calls
``ticket.ship()`` directly on the REVIEWED-state branch without running
``Session.check_gate_across_ticket("shipping")`` first. A REVIEWED ticket whose
sessions never recorded ``testing`` and ``reviewing`` therefore advances to
SHIPPED through the task path, even though the same ticket would be blocked
on the ``pr create`` path.

The fix is to gate the task-based path through the *same* check
``_check_shipping_gate`` uses (``Session.check_gate_across_ticket``), so the
two completion paths cannot disagree on what "earns" a SHIPPED state.
"""

import pytest
from django.test import TestCase

from teatree.core.models import Session, Task, Ticket
from teatree.core.models.errors import QualityGateError


class TestShippingTaskHonorsVisitedPhasesGate(TestCase):
    """A shipping task on a REVIEWED ticket with empty visited_phases must NOT advance."""

    def test_complete_shipping_task_without_phase_attestations_raises_gate_error(self) -> None:
        # Reviewed ticket with NO recorded testing/reviewing phase visits —
        # the FSM walked here via direct transition (bypassing the loop)
        # but the gate's source of truth (Session.visited_phases) is empty.
        ticket = Ticket.objects.create(
            overlay="test",
            state=Ticket.State.REVIEWED,
        )
        session = Session.objects.create(ticket=ticket, agent_id="t")
        # No visit_phase() calls — visited_phases is []
        assert session.visited_phases == []

        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="shipping",
            status=Task.Status.COMPLETED,
        )

        # Pre-fix: task._apply_phase_transition() silently calls ticket.ship()
        # and the ticket advances to SHIPPED.  Post-fix: the same gate the
        # ``pr create`` path enforces (check_gate_across_ticket) raises
        # QualityGateError because ``testing`` and ``reviewing`` are missing.
        with pytest.raises(QualityGateError, match=r"testing|reviewing"):
            task._apply_phase_transition()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED, (
            f"ticket must stay REVIEWED when shipping gate fails, got {ticket.state}"
        )

    def test_complete_shipping_task_with_attestations_still_ships(self) -> None:
        # Sanity: happy path must keep working. A REVIEWED ticket whose
        # session DID record testing+reviewing must still advance to SHIPPED.
        ticket = Ticket.objects.create(
            overlay="test",
            state=Ticket.State.REVIEWED,
        )
        session = Session.objects.create(ticket=ticket, agent_id="t")
        session.visit_phase("testing", agent_id="t")
        session.visit_phase("reviewing", agent_id="t")
        session.refresh_from_db()

        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="shipping",
            status=Task.Status.COMPLETED,
        )

        fired = task._apply_phase_transition()

        ticket.refresh_from_db()
        assert fired is True
        assert ticket.state == Ticket.State.SHIPPED
