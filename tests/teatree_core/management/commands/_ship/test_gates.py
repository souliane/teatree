"""``pr create``'s shipping gate refuses a pre-PR ticket whose latest self-review held."""

from django.test import TestCase

from teatree.core.management.commands._ship.gates import check_shipping_gate
from teatree.core.models import Session, Ticket
from tests.teatree_core._self_review_helpers import HELD_SHA, author_ticket, completed_self_review


def _attested_ticket(state: str = Ticket.State.TESTED) -> Ticket:
    ticket = author_ticket(state=state)
    session = Session.objects.create(ticket=ticket)
    session.visit_phase("testing")
    session.visit_phase("reviewing")
    return ticket


class TestAHeldSelfReviewBlocksShipping(TestCase):
    def test_a_pre_pr_hold_is_refused_naming_the_task_and_head(self) -> None:
        ticket = _attested_ticket()
        held = completed_self_review(ticket, "hold")

        failure = check_shipping_gate(ticket)

        assert failure is not None
        assert failure["allowed"] is False
        assert f"reviewing task {held.pk}" in failure["error"]
        assert HELD_SHA in failure["error"]
        assert f"rework-hold {ticket.pk}" in failure["hint"]
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.TESTED

    def test_a_merge_safe_self_review_reaches_self_reviewed(self) -> None:
        ticket = _attested_ticket()
        completed_self_review(ticket, "hold")
        completed_self_review(ticket, "merge_safe")

        assert check_shipping_gate(ticket) is None
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SELF_REVIEWED

    def test_a_ticket_with_its_pr_open_is_never_refused_for_a_hold(self) -> None:
        ticket = _attested_ticket(state=Ticket.State.PR_OPENED)
        completed_self_review(ticket, "hold")

        assert check_shipping_gate(ticket) is None

    def test_a_hold_from_the_shipped_cycle_does_not_refuse_the_follow_up(self) -> None:
        ticket = _attested_ticket(state=Ticket.State.DELIVERED)
        completed_self_review(ticket, "hold")
        ticket.reopen_for_followup()
        ticket.save()

        assert check_shipping_gate(ticket) is None
