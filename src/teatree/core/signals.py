import logging

from django_fsm.signals import post_transition

from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)


def _log_ticket_transition(
    sender: type,  # noqa: ARG001
    instance: Ticket,
    name: str,
    source: str,
    target: str,
    **_kwargs: object,
) -> None:
    from teatree.core.models.transition import TicketTransition  # noqa: PLC0415

    session = instance.sessions.order_by("-started_at").first()
    TicketTransition.objects.create(
        ticket=instance,
        session=session,
        from_state=source,
        to_state=target,
        triggered_by=name,
    )


def register_signals() -> None:
    post_transition.connect(_log_ticket_transition, sender=Ticket, dispatch_uid="ticket_transition_audit")
