"""Mechanical action handlers — inline ticket transitions executed during a tick.

Each handler receives an ``ActionPayload`` dict and mutates the DB directly.
Called by ``tick._execute_mechanical`` after dispatch, before statusline render.
"""

import logging
from collections.abc import Callable

from teatree.loop.dispatch import ActionPayload

logger = logging.getLogger(__name__)


def ignore_disposed_ticket(payload: ActionPayload) -> None:
    from django.apps import apps  # noqa: PLC0415

    ticket_model = apps.get_model("core", "Ticket")
    ticket_id = payload.get("ticket_id")
    if ticket_id is None:
        return
    ticket = ticket_model.objects.get(pk=ticket_id)
    ticket.ignore()
    ticket.save()
    logger.info("Auto-ignored ticket %s (reason: %s)", ticket_id, payload.get("reason", "?"))


def complete_ticket(payload: ActionPayload) -> None:
    """Transition a ticket from its current post-ship state toward delivered.

    FSM path: shipped → request_review → mark_merged → retrospect.
    """
    from django.apps import apps  # noqa: PLC0415

    ticket_model = apps.get_model("core", "Ticket")
    ticket_id = payload.get("ticket_id")
    if ticket_id is None:
        return
    ticket = ticket_model.objects.get(pk=ticket_id)

    if ticket.state == "shipped":
        ticket.request_review()
        ticket.save()
    if ticket.state == "in_review":
        ticket.mark_merged()
        ticket.save()
    if ticket.state == "merged":
        ticket.retrospect()
        ticket.save()


def reopen_ticket(payload: ActionPayload) -> None:
    from django.apps import apps  # noqa: PLC0415

    ticket_model = apps.get_model("core", "Ticket")
    ticket_id = payload.get("ticket_id")
    if ticket_id is None:
        return
    ticket = ticket_model.objects.get(pk=ticket_id)
    ticket.reopen()
    ticket.save()
    logger.info("Auto-reopened ticket %s (was %s, draft MRs detected)", ticket_id, payload.get("ticket_state", "?"))


HANDLERS: dict[str, Callable[[ActionPayload], None]] = {
    "ticket_disposition": ignore_disposed_ticket,
    "ticket_completion": complete_ticket,
    "ticket_reopen": reopen_ticket,
}
