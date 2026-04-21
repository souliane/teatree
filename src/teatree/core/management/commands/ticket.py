"""Ticket state management: transitions and listing, mirroring dashboard actions."""

from django_fsm import TransitionNotAllowed
from django_typer.management import TyperCommand, command

from teatree.core.models import Ticket

_ALLOWED_TRANSITIONS = {
    "scope",
    "start",
    "code",
    "test",
    "review",
    "ship",
    "request_review",
    "mark_merged",
    "retrospect",
    "mark_delivered",
    "rework",
}


class Command(TyperCommand):
    @command()
    def transition(self, ticket_id: int, transition_name: str) -> dict[str, object]:
        """Transition a ticket to a new state.

        Accepts any of the allowed transition names: scope, start, code, test,
        review, ship, request_review, mark_merged, retrospect, mark_delivered, rework.
        """
        if transition_name not in _ALLOWED_TRANSITIONS:
            return {"error": f"Unknown transition: {transition_name}"}

        try:
            ticket = Ticket.objects.get(pk=ticket_id)
        except Ticket.DoesNotExist:
            return {"error": f"Ticket {ticket_id} not found"}

        method = getattr(ticket, transition_name, None)
        if method is None:
            return {"error": f"Invalid transition: {transition_name}"}

        try:
            method()
            ticket.save()
        except TransitionNotAllowed:
            return {
                "error": f"Transition '{transition_name}' not allowed from state '{ticket.state}'",
            }

        return {"ticket_id": int(ticket.pk), "state": ticket.state}

    @command(name="list")
    def list_tickets(self, state: str = "", overlay: str = "") -> list[dict[str, object]]:
        """List tickets, optionally filtered by state and/or overlay."""
        qs = Ticket.objects.order_by("-pk")
        if state:
            qs = qs.filter(state=state)
        if overlay:
            qs = qs.filter(overlay=overlay)
        return [
            {
                "id": int(ticket.pk),
                "state": ticket.state,
                "overlay": ticket.overlay,
                "issue_url": ticket.issue_url,
                "variant": ticket.variant,
            }
            for ticket in qs
        ]
