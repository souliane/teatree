"""A PR chip must say something an operator can act on (#3909).

The chip is the board's answer to "what are this ticket's PRs". It rendered the FSM
slug verbatim — ``review_requested`` — and gave every chip the same styling, so a
merged PR, a PR waiting on review and a PR closed without merging were three
identical-looking chips distinguished only by a lowercase token with an underscore
in it. The row already carries a human label for its state; the chip should use it,
and should carry the state as a class so the three read apart at a glance.
"""

from django.test import TestCase
from django.urls import reverse

from teatree.core.models import PullRequest, Ticket
from teatree.dash.selectors import build_kanban_columns
from tests.factories import PullRequestFactory, TicketFactory

State = Ticket.State
_LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}


def _chips(ticket: Ticket) -> tuple:
    board = build_kanban_columns()
    for group in board.groups:
        for column in group.columns:
            for card in column.cards:
                if card.ticket_id == ticket.pk:
                    return card.pr_chips
    return ()


class ChipCarriesAHumanStateTestCase(TestCase):
    def test_a_review_requested_chip_reads_as_words_not_a_slug(self) -> None:
        ticket = TicketFactory(state=State.SHIPPED)
        PullRequestFactory(ticket=ticket, repo="acme-org/backend", iid="7", state=PullRequest.State.REVIEW_REQUESTED)
        chip = _chips(ticket)[0]
        assert chip.label == "Review requested"
        assert chip.state == PullRequest.State.REVIEW_REQUESTED

    def test_a_closed_chip_is_labelled_closed(self) -> None:
        ticket = TicketFactory(state=State.SHIPPED)
        PullRequestFactory(ticket=ticket, repo="acme-org/backend", iid="8", state=PullRequest.State.CLOSED)
        assert _chips(ticket)[0].label == "Closed"

    def test_an_unknown_state_falls_back_to_the_raw_value(self) -> None:
        ticket = TicketFactory(state=State.SHIPPED)
        row = PullRequestFactory(ticket=ticket, repo="acme-org/backend", iid="9")
        PullRequest.objects.filter(pk=row.pk).update(state="something_new")
        assert _chips(ticket)[0].label == "something_new"


class ChipStatesReadApartOnTheBoardTestCase(TestCase):
    def test_a_merged_chip_carries_its_state_as_a_class(self) -> None:
        ticket = TicketFactory(state=State.SHIPPED)
        PullRequestFactory(ticket=ticket, repo="acme-org/backend", iid="7", state=PullRequest.State.MERGED)
        body = self.client.get(reverse("dash:board"), **_LOOPBACK).content.decode()
        assert 'class="chip pr merged"' in body
        assert "Merged" in body

    def test_an_open_chip_carries_its_own_class(self) -> None:
        ticket = TicketFactory(state=State.SHIPPED)
        PullRequestFactory(ticket=ticket, repo="acme-org/backend", iid="7", state=PullRequest.State.OPEN)
        body = self.client.get(reverse("dash:board"), **_LOOPBACK).content.decode()
        assert 'class="chip pr open"' in body

    def test_the_slug_never_reaches_the_page(self) -> None:
        ticket = TicketFactory(state=State.SHIPPED)
        PullRequestFactory(ticket=ticket, repo="acme-org/backend", iid="7", state=PullRequest.State.REVIEW_REQUESTED)
        body = self.client.get(reverse("dash:board"), **_LOOPBACK).content.decode()
        assert "· review_requested" not in body
        assert "· Review requested" in body
