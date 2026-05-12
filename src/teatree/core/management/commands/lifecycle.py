"""Lifecycle and session phase operations."""

import logging

from django.db import transaction
from django_fsm import TransitionNotAllowed
from django_typer.management import TyperCommand, command, initialize

from teatree.core.models import Session, Ticket

logger = logging.getLogger(__name__)

_PHASE_TO_TRANSITION: dict[str, str] = {
    "scoping": "scope",
    "coding": "start",
    "testing": "test",
    "reviewing": "review",
    "shipping": "ship",
    "requesting_review": "request_review",
    "retrospecting": "retrospect",
}


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """Group root — forces sub-commands to be addressed by name."""

    @command(name="visit-phase")
    def visit_phase(self, ticket_id: int, phase: str) -> str:
        """Mark a phase as visited and advance the ticket FSM if applicable."""
        ticket = Ticket.objects.get(pk=ticket_id)
        session = ticket.sessions.order_by("-pk").first()
        if session is None:
            session = Session.objects.create(ticket=ticket)
        session.visit_phase(phase)

        transition_name = _PHASE_TO_TRANSITION.get(phase)
        if transition_name:
            _try_advance(ticket, transition_name)

        return f"Phase '{phase}' marked as visited on session {session.pk} (ticket state: {ticket.state})"


def _try_advance(ticket: Ticket, transition_name: str) -> None:
    method = getattr(ticket, transition_name, None)
    if method is None:
        return
    try:
        with transaction.atomic():
            method()
            ticket.save()
    except TransitionNotAllowed:
        logger.debug(
            "Transition '%s' not valid from state '%s' for ticket %s — FSM unchanged",
            transition_name,
            ticket.state,
            ticket.pk,
        )
