"""Lifecycle and session phase operations."""

from django_typer.management import TyperCommand, command, initialize

from teatree.core.models import Session, Ticket


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """Group root — forces sub-commands to be addressed by name."""

    @command(name="visit-phase")
    def visit_phase(self, ticket_id: int, phase: str) -> str:
        """Mark a phase as visited on the ticket's latest session."""
        ticket = Ticket.objects.get(pk=ticket_id)
        session = ticket.sessions.order_by("-pk").first()
        if session is None:
            session = Session.objects.create(ticket=ticket)
        session.visit_phase(phase)
        return f"Phase '{phase}' marked as visited on session {session.pk}"
