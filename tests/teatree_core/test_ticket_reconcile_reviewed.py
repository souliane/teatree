"""``Ticket.reconcile_reviewed`` — gate-driven FSM catch-up (#694).

The shipping gate verifies the required phases on ``Session.visited_phases``
(the single source of truth) and then advances the FSM to REVIEWED so
``ship()`` is legal. This transition is the FSM-level expression of that
reconciliation: any pre-REVIEWED state -> REVIEWED, no task conditions
(the gate already attested the work via the session record).
"""

import pytest
from django.test import TestCase
from django_fsm import TransitionNotAllowed

from teatree.core.models import Ticket


class TestReconcileReviewed(TestCase):
    def test_started_reconciles_to_reviewed(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)
        ticket.reconcile_reviewed()
        ticket.save()
        assert ticket.state == Ticket.State.REVIEWED

    def test_not_started_reconciles_to_reviewed(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.NOT_STARTED)
        ticket.reconcile_reviewed()
        ticket.save()
        assert ticket.state == Ticket.State.REVIEWED

    def test_reviewed_is_idempotent(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        ticket.reconcile_reviewed()
        ticket.save()
        assert ticket.state == Ticket.State.REVIEWED

    def test_post_ship_state_cannot_reconcile_backwards(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.SHIPPED)
        with pytest.raises(TransitionNotAllowed):
            ticket.reconcile_reviewed()
