"""Tickets that merged a PR and were then reopened — the shape board-reconcile rule A used to undo (#5031)."""

from teatree.core.models import PullRequest, Ticket
from tests.factories import MergeAuditFactory, PullRequestFactory


def merged_ticket(*, state: str = Ticket.State.MERGED, issue_url: str = "") -> Ticket:
    """A ticket with a MERGED PR row and the merge evidence the FSM gate asks for, as ticket 1672 carries."""
    ticket = Ticket.objects.create(overlay="t3-teatree", state=state, issue_url=issue_url)
    PullRequestFactory(ticket=ticket, state=PullRequest.State.MERGED)
    MergeAuditFactory(clear__ticket=ticket)
    return ticket


def reopened_ticket(*, followup: bool = False) -> Ticket:
    ticket = merged_ticket()
    if followup:
        ticket.reopen_for_followup()
    else:
        ticket.reopen()
    ticket.save()
    return ticket
