"""Lifecycle and session phase operations."""

import logging
from typing import Annotated

import typer
from django.db import transaction
from django_fsm import TransitionNotAllowed
from django_typer.management import TyperCommand, command, initialize

from teatree.core.db_anchor import assert_lifecycle_db_is_canonical
from teatree.core.models import Ticket
from teatree.core.phases import normalize_phase, phase_transition

logger = logging.getLogger(__name__)


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """Group root — forces sub-commands to be addressed by name."""

    @command(name="visit-phase")
    def visit_phase(
        self,
        ticket_id: str,
        phase: str,
        agent_id: Annotated[
            str,
            typer.Option(help="Recording agent identity stamped into phase_visits (maker≠checker attribution)."),
        ] = "",
    ) -> str:
        """Mark a phase as visited and advance the ticket FSM if applicable.

        ``ticket_id`` accepts the same identifier set as ``pr create`` — DB
        pk, forge issue number, or full issue URL (#694). The phase is
        normalized to the canonical vocabulary so both the short verbs the
        skills emit (``code``, ``test``, ``review``, ``ship``, ``retro``,
        ``scope``) and the older gerunds advance the FSM. The resulting
        ``ticket.state`` is included in the output so a skipped or refused
        transition is visible rather than silently swallowed.

        ``--agent-id`` records the recording agent's identity for
        maker≠checker (#755). Resolution is delegated to
        ``Session.recording_identity`` so the attribution is **never
        empty** even when neither ``--agent-id`` nor ``Session.agent_id``
        is set — a blank previously made the gate vacuously pass.
        """
        ticket = Ticket.objects.resolve(ticket_id)
        # #779: refuse to record a phase into a worktree-isolated DB the
        # shipping gate never reads. Run BEFORE any write so the attestation
        # is never split from the DB `pr create` consults — symmetric across
        # maker (testing/retro) and reviewer (reviewing) visits.
        assert_lifecycle_db_is_canonical(ticket)
        canonical = normalize_phase(phase)
        # #801 SSOT: the canonical earliest+locked policy — never the
        # old -pk-latest pick nor a raw blank-agent_id create (which
        # failed _check_maker_checker closed). The explicit --agent-id
        # seeds a created session's identity.
        session = ticket.resolve_phase_session(agent_id=agent_id or "loop")
        session.visit_phase(canonical, agent_id=session.recording_identity(agent_id))

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
