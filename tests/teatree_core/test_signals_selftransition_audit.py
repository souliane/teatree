"""A state-preserving transition writes no ``TicketTransition`` audit row (#3876).

Several transitions deliberately list their own target in ``source`` so a re-run
self-loops instead of raising — ``mark_reviewed_externally`` does it so a moved head
SHA re-stamps ``last_review_state`` and the ticket stays in the re-review watch set.
That makes them idempotent in STATE but, before this guard, not in side effects:
every re-run still fired the audit receiver.

A caller re-running one per pass therefore wrote one row per ticket per pass forever.
Measured on the live box: 3,240,987 of 3,241,397 rows (99.99%) were
``review_posted → review_posted``, all from ``mark_reviewed_externally``, still growing
at ~410/min — ~85% of the control DB, and the control DB is the seed copied into every
per-worktree env dir.

The self-transition itself must keep working; only the empty audit row goes.
"""

from django.db.models import F
from django.test import TestCase

from teatree.core.models import Session, Task, Ticket
from teatree.core.models.transition import TicketTransition


class SelfTransitionIsNotAnAuditEvent(TestCase):
    """The audit table records state EDGES; a self-transition has none."""

    def _reviewed_ticket(self) -> Ticket:
        """A reviewer ticket already at REVIEW_POSTED, as the scanner finds it each pass."""
        ticket = Ticket.objects.create(
            overlay="test",
            role=Ticket.Role.REVIEWER,
            state=Ticket.State.REVIEW_POSTED,
        )
        session = Session.objects.create(ticket=ticket, agent_id="t")
        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            status=Task.Status.COMPLETED,
        )
        return ticket

    def test_re_marking_an_already_reviewed_ticket_adds_no_row(self) -> None:
        """The regression: this call shape wrote 3.2M rows."""
        ticket = self._reviewed_ticket()
        before = TicketTransition.objects.filter(ticket=ticket).count()

        for _ in range(5):
            ticket.mark_reviewed_externally()

        assert TicketTransition.objects.filter(ticket=ticket).count() == before, (
            "a re-review that changes no state must not append an audit row"
        )

    def test_the_self_transition_still_happens(self) -> None:
        """Anti-vacuity for the FIX: suppressing the row must not suppress the transition.

        The self-loop is load-bearing — without it ``Task.complete()``'s derived-source
        guard skips the FSM advance and the ticket drops out of the re-review watch set.
        A 'fix' that removed the transition would pass the row assertion above and break
        re-review, so it is pinned here.
        """
        ticket = self._reviewed_ticket()
        ticket.mark_reviewed_externally()
        assert ticket.state == Ticket.State.REVIEW_POSTED

    def test_no_audit_row_anywhere_has_equal_from_and_to(self) -> None:
        """Stated as the invariant, so a future self-transition is covered without edits."""
        ticket = self._reviewed_ticket()
        for _ in range(3):
            ticket.mark_reviewed_externally()

        assert not TicketTransition.objects.filter(from_state=F("to_state")).exists(), (
            "no audit row may have from_state == to_state"
        )

    def test_control_a_real_state_change_is_still_recorded(self) -> None:
        """Anti-vacuity: the guard must not silence genuine transitions."""
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.NOT_STARTED)
        before = TicketTransition.objects.filter(ticket=ticket).count()

        ticket.ignore()

        rows = TicketTransition.objects.filter(ticket=ticket)
        assert rows.count() == before + 1, "a genuine state change must still be audited"
        latest = rows.order_by("-id").first()
        assert latest is not None
        assert latest.from_state != latest.to_state
