"""``TicketTransition``'s prune predicate — decided per ROW, keyed on ticket closure (#3871).

The rule this pins: a ``from_state == to_state`` row records no edge, so it is not
history and a reopened ticket does not need it; every real state edge survives for as
long as the ticket does. On top of that sit three guards, each with a test that goes RED
when the guard is dropped — an OPEN ticket's rows are never touched, and neither is a
ticket's earliest row (the creation proxy ``factory_signal_queries`` dates a fix ticket
by via ``Min(created_at)``) or its latest (the last-activity signal the stale-ticket and
stuck-redispatch scanners read as ``Max(created_at)``).
"""

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Ticket
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.transition import TicketTransition, TicketTransitionQuerySet

_OLD = timezone.now() - dt.timedelta(days=120)
_RECENT = timezone.now() - dt.timedelta(days=2)

#: ``(from_state, to_state, triggered_by)``. ``_NOOP`` is the state-preserving
#: self-transition ``mark_reviewed_externally`` re-fires; #3876 stopped writing it, and
#: this lane is the standing backstop for the residue.
_MOVE = (Ticket.State.WORK_STARTED, Ticket.State.CODED, "code")
_ENTER_REVIEW_POSTED = (Ticket.State.WORK_STARTED, Ticket.State.REVIEW_DELIVERED, "mark_reviewed_externally")
_NOOP = (Ticket.State.REVIEW_DELIVERED, Ticket.State.REVIEW_DELIVERED, "mark_reviewed_externally")


def _transition(
    *,
    ticket: Ticket,
    created_at: dt.datetime = _OLD,
    move: tuple[str, str, str] = _MOVE,
    session: Session | None = None,
) -> TicketTransition:
    from_state, to_state, triggered_by = move
    row = TicketTransition.objects.create(
        ticket=ticket,
        session=session,
        from_state=from_state,
        to_state=to_state,
        triggered_by=triggered_by,
    )
    # created_at is auto_now_add — age it with a direct UPDATE.
    TicketTransition.objects.filter(pk=row.pk).update(created_at=created_at)
    return TicketTransition.objects.get(pk=row.pk)


def _noop(ticket: Ticket, *, created_at: dt.datetime = _OLD) -> TicketTransition:
    return _transition(ticket=ticket, created_at=created_at, move=_NOOP)


class StateEdgesTestCase(TestCase):
    def test_the_manager_exposes_the_lane_queryset(self) -> None:
        assert isinstance(TicketTransition.objects.all(), TicketTransitionQuerySet)

    def test_a_move_is_an_edge_and_a_self_transition_is_not(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.MERGED)
        edge = _transition(ticket=ticket)
        _noop(ticket, created_at=_RECENT)
        assert list(TicketTransition.objects.state_edges().values_list("pk", flat=True)) == [edge.pk]

    def test_recent_merges_deduplicates_remerged_tickets_at_first_merge(self) -> None:
        first = Ticket.objects.create(state=Ticket.State.MERGED)
        second = Ticket.objects.create(state=Ticket.State.MERGED)
        for ticket in (first, second):
            PullRequest.objects.create(
                ticket=ticket,
                url=f"https://example.test/pull/{ticket.pk}",
                repo="example/repo",
                iid=str(ticket.pk),
                state=PullRequest.State.MERGED,
            )
        first_stamp = _RECENT - dt.timedelta(hours=2)
        second_stamp = _RECENT - dt.timedelta(hours=1)
        _transition(ticket=first, created_at=first_stamp, move=(Ticket.State.PR_OPENED, Ticket.State.MERGED, "merge"))
        _transition(ticket=first, created_at=_RECENT, move=(Ticket.State.PR_OPENED, Ticket.State.MERGED, "remerge"))
        _transition(ticket=second, created_at=second_stamp, move=(Ticket.State.PR_OPENED, Ticket.State.MERGED, "merge"))
        assert list(TicketTransition.objects.recent_merges(10)) == [
            (second.pk, second_stamp),
            (first.pk, first_stamp),
        ]

    def test_recent_merges_excludes_a_ticket_without_a_merged_pr(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.MERGED)
        _transition(ticket=ticket, move=(Ticket.State.PR_OPENED, Ticket.State.MERGED, "merge"))
        assert list(TicketTransition.objects.recent_merges(10)) == []


class TicketTransitionPrunableGuardTestCase(TestCase):
    def _closed_ticket_with_three_noops(self) -> tuple[Ticket, list[TicketTransition]]:
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.REVIEW_DELIVERED)
        rows = [_noop(ticket, created_at=_OLD + dt.timedelta(minutes=n)) for n in range(3)]
        return ticket, rows

    def test_a_middle_noop_of_a_closed_ticket_is_prunable(self) -> None:
        _, rows = self._closed_ticket_with_three_noops()
        assert list(TicketTransition.objects.prunable().values_list("pk", flat=True)) == [rows[1].pk]

    def test_a_state_edge_is_never_prunable(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.MERGED)
        for n in range(3):
            _transition(ticket=ticket, created_at=_OLD + dt.timedelta(minutes=n))
        assert TicketTransition.objects.prunable().count() == 0

    def test_an_open_tickets_noop_is_never_prunable(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.CODED)
        for n in range(3):
            _noop(ticket, created_at=_OLD + dt.timedelta(minutes=n))
        assert TicketTransition.objects.prunable().count() == 0

    def test_a_shipped_tickets_noop_is_never_prunable(self) -> None:
        """PR_OPENED is not closed — its PR is still open, so the ticket may take rework."""
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.PR_OPENED)
        for n in range(3):
            _noop(ticket, created_at=_OLD + dt.timedelta(minutes=n))
        assert TicketTransition.objects.prunable().count() == 0

    def test_the_earliest_row_is_never_prunable(self) -> None:
        _, rows = self._closed_ticket_with_three_noops()
        assert rows[0].pk not in set(TicketTransition.objects.prunable().values_list("pk", flat=True))

    def test_the_latest_row_is_never_prunable(self) -> None:
        _, rows = self._closed_ticket_with_three_noops()
        assert rows[-1].pk not in set(TicketTransition.objects.prunable().values_list("pk", flat=True))

    def test_a_sole_noop_is_never_prunable(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.REVIEW_DELIVERED)
        _noop(ticket)
        assert TicketTransition.objects.prunable().count() == 0

    def test_a_same_timestamp_burst_still_keeps_both_boundaries(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.REVIEW_DELIVERED)
        stamp = timezone.now()
        rows = [_noop(ticket, created_at=stamp) for _ in range(4)]
        assert set(TicketTransition.objects.prunable().values_list("pk", flat=True)) == {rows[1].pk, rows[2].pk}

    def test_another_tickets_rows_do_not_supply_the_boundary(self) -> None:
        """The boundary is per TICKET — a sibling's rows must not make these prunable."""
        _, rows = self._closed_ticket_with_three_noops()
        other = Ticket.objects.create(overlay="acme", state=Ticket.State.REVIEW_DELIVERED)
        for n in range(3):
            _noop(other, created_at=_RECENT + dt.timedelta(minutes=n))
        prunable = set(TicketTransition.objects.prunable().values_list("pk", flat=True))
        assert rows[0].pk not in prunable
        assert rows[-1].pk not in prunable
