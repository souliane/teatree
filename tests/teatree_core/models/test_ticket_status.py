import pytest
from django.test import TestCase
from django_fsm import TransitionNotAllowed

from teatree.core.models import Session, Ticket
from teatree.core.models.transition import TicketTransition
from tests.factories import MergeVoidFactory, merge_void

State = Ticket.State


def _merged_ticket(*, state: str = State.MERGED, edges: tuple[tuple[str, str], ...] = ()) -> Ticket:
    ticket = Ticket.objects.create(overlay="t3-teatree", state=state)
    for from_state, to_state in edges or ((State.CODED, State.MERGED),):
        TicketTransition.objects.create(
            ticket=ticket, from_state=from_state, to_state=to_state, triggered_by="reconcile_merged"
        )
    return ticket


class TestTicketVoidMerge(TestCase):
    def test_restores_the_newest_pre_merge_state_and_records_the_edge(self) -> None:
        ticket = _merged_ticket(
            edges=(
                (State.CODED, State.MERGED),
                (State.MERGED, State.WORK_STARTED),
                (State.WORK_STARTED, State.MERGED),
            )
        )
        void = merge_void(tickets=[ticket])

        restored = ticket.void_merge(void)
        ticket.save()

        ticket.refresh_from_db()
        assert restored == State.WORK_STARTED
        assert ticket.state == State.WORK_STARTED
        newest = ticket.transitions.order_by("-created_at", "-pk").first()
        assert (newest.from_state, newest.to_state, newest.triggered_by) == (
            State.MERGED,
            State.WORK_STARTED,
            "void_merge",
        )
        assert ticket.transitions.count() == 4

    def test_a_ticket_past_merged_restores_from_its_edge_into_merged(self) -> None:
        ticket = _merged_ticket(
            state=State.RETRO_RECORDED,
            edges=((State.TESTED, State.MERGED), (State.MERGED, State.RETRO_RECORDED)),
        )
        void = merge_void(tickets=[ticket])

        assert ticket.void_merge(void) == State.TESTED
        assert ticket.state == State.TESTED

    def test_refuses_without_a_recorded_void_naming_the_ticket(self) -> None:
        ticket = _merged_ticket()
        unsaved = MergeVoidFactory.build()
        naming_another = merge_void(tickets=[Ticket.objects.create(overlay="t3-teatree")])

        for void in (unsaved, naming_another):
            with self.subTest(void=void.pk), pytest.raises(TransitionNotAllowed):
                ticket.void_merge(void)

        assert ticket.state == State.MERGED
        assert ticket.transitions.count() == 1

    def test_refuses_a_ticket_that_is_not_merged(self) -> None:
        ticket = _merged_ticket(state=State.REVIEW_REQUESTED)
        void = merge_void(tickets=[ticket])

        with pytest.raises(TransitionNotAllowed):
            ticket.void_merge(void)

    def test_refuses_a_ticket_with_no_edge_into_merged(self) -> None:
        ticket = _merged_ticket(edges=((State.WORK_STARTED, State.CODED),))
        void = merge_void(tickets=[ticket])

        with pytest.raises(TransitionNotAllowed):
            ticket.void_merge(void)

        assert ticket.state == State.MERGED

    def test_keeps_the_phase_ledger_and_extra(self) -> None:
        ticket = _merged_ticket()
        ticket.extra = {"forge_merge_evidence": {"slug": "example-org/example-repo", "pr_id": 41}}
        ticket.save()
        session = Session.objects.create(
            ticket=ticket, overlay="t3-teatree", visited_phases=["coding"], phase_visits={"coding": "coder"}
        )
        void = merge_void(tickets=[ticket])

        ticket.void_merge(void)
        ticket.save()

        session.refresh_from_db()
        ticket.refresh_from_db()
        assert session.visited_phases == ["coding"]
        assert session.phase_visits == {"coding": "coder"}
        assert ticket.extra["forge_merge_evidence"] == {"slug": "example-org/example-repo", "pr_id": 41}
        assert ticket.transitions.order_by("-created_at", "-pk").first().session == session
