"""Lifecycle and session phase operations."""

import logging

from django.db import transaction
from django_fsm import TransitionNotAllowed
from django_typer.management import TyperCommand, command, initialize

from teatree.core.models import Session, Ticket
from teatree.core.phases import normalize_phase, phase_transition

logger = logging.getLogger(__name__)


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """Group root — forces sub-commands to be addressed by name."""

    @command(name="visit-phase")
    def visit_phase(self, ticket_id: str, phase: str) -> str:
        """Mark a phase as visited and advance the ticket FSM if applicable.

        ``ticket_id`` accepts the same identifier set as ``pr create`` — DB
        pk, forge issue number, or full issue URL (#694). The phase is
        normalized to the canonical vocabulary so both the short verbs the
        skills emit (``code``, ``test``, ``review``, ``ship``, ``retro``,
        ``scope``) and the older gerunds advance the FSM. The resulting
        ``ticket.state`` is included in the output so a skipped or refused
        transition is visible rather than silently swallowed.
        """
        ticket = Ticket.objects.resolve(ticket_id)
        canonical = normalize_phase(phase)
        session = ticket.sessions.order_by("-pk").first()
        if session is None:
            session = Session.objects.create(ticket=ticket)
        # Thread the session's identity, symmetric with the loop path
        # (Task._record_phase_visit). Without an ``agent_id`` the phase
        # never lands in ``phase_visits`` and ``_check_maker_checker``
        # silently skips it — a CLI ``visit-phase reviewing`` would then
        # evade the maker≠checker pairing (#694, review nit 2).
        session.visit_phase(canonical, agent_id=session.agent_id)

        transition_name = phase_transition(canonical)
        if transition_name:
            _try_advance(ticket, transition_name)

        return f"Phase '{canonical}' marked as visited on session {session.pk} (ticket state: {ticket.state})"


def _try_advance(ticket: Ticket, transition_name: str) -> None:
    # ``phase_transition`` only ever returns the name of a real ``Ticket``
    # FSM transition, so ``getattr`` always resolves here.
    method = getattr(ticket, transition_name)
    try:
        with transaction.atomic():
            method()
            ticket.save()
    except TransitionNotAllowed:
        # Loud, not swallowed (#694): an out-of-order / skipped transition
        # used to vanish at DEBUG and only resurface as a raw
        # TransitionNotAllowed at `pr create`. The phase visit is still
        # recorded; the shipping gate reconciles the FSM from it later.
        logger.warning(
            "Transition '%s' not valid from state '%s' for ticket %s — "
            "FSM unchanged; phase visit recorded, gate will reconcile",
            transition_name,
            ticket.state,
            ticket.pk,
        )
