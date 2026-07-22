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

    def test_in_review_reconciles_to_reviewed(self) -> None:
        """IN_REVIEW reconciles back to REVIEWED so a stranded ticket can re-ship (#798).

        A failed/incomplete prior ship leaves the ticket at IN_REVIEW with
        no PR; reconciling it lets the gate-passing ticket re-ship. SHIPPED
        stays terminal (genuine post-ship success).
        """
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IN_REVIEW)
        ticket.reconcile_reviewed()
        ticket.save()
        assert ticket.state == Ticket.State.REVIEWED

    def test_retrospected_reconciles_to_reviewed(self) -> None:
        """#808: RETROSPECTED is non-terminal and must reconcile to REVIEWED.

        The enumerated source list (pre-#808) omitted RETROSPECTED, so a
        re-provisioned ticket whose FSM lingered there denied ``pr create``
        with ``{'allowed': False, 'missing': []}`` even though its phase
        ledger satisfied the gate. Phase-driven reconcile: any non-terminal
        state reconciles.
        """
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.RETROSPECTED)
        ticket.reconcile_reviewed()
        ticket.save()
        assert ticket.state == Ticket.State.REVIEWED

    def test_every_non_terminal_state_reconciles_to_reviewed(self) -> None:
        """State-complete: EVERY non-terminal state reconciles (#808).

        Pins the contract so a future added state cannot silently re-break
        the gate (the recurring #798/#799/#808 class). Only SHIPPED /
        MERGED / DELIVERED / REVIEW_POSTED / IGNORED are terminal.
        """
        terminal = {
            Ticket.State.SHIPPED,
            Ticket.State.MERGED,
            Ticket.State.DELIVERED,
            Ticket.State.REVIEW_POSTED,
            Ticket.State.IGNORED,
        }
        non_terminal = [s for s in Ticket.State if s not in terminal]
        for state in non_terminal:
            ticket = Ticket.objects.create(overlay="test", state=state)
            ticket.reconcile_reviewed()
            ticket.save()
            assert ticket.state == Ticket.State.REVIEWED, f"{state} did not reconcile"

    def test_terminal_states_cannot_reconcile_backwards(self) -> None:
        """MERGED / DELIVERED / REVIEW_POSTED / IGNORED stay terminal alongside SHIPPED."""
        for state in (
            Ticket.State.MERGED,
            Ticket.State.DELIVERED,
            Ticket.State.REVIEW_POSTED,
            Ticket.State.IGNORED,
        ):
            ticket = Ticket.objects.create(overlay="test", state=state)
            with pytest.raises(TransitionNotAllowed):
                ticket.reconcile_reviewed()

    def test_source_set_is_complete_partition_of_all_states(self) -> None:
        """#808 structural guard: reconcile-source union terminal == ALL states.

        The reconcile source is an explicit list (class-body comprehensions
        can't see ``State``). This asserts the partition is exhaustive so a
        FUTURE added State member is caught HERE (at the list) instead of
        silently re-introducing the enumerated-source `{'allowed': False,
        'missing': []}` recurrence.
        """
        all_states = set(Ticket.State)
        source = set(Ticket._RECONCILE_SOURCE_STATES)
        terminal = set(Ticket._TERMINAL_STATES)
        assert source.isdisjoint(terminal), "a state is both reconcilable and terminal"
        assert source | terminal == all_states, (
            f"State partition incomplete — unclassified: {all_states - source - terminal}"
        )
