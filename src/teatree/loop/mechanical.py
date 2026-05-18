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


def reviewer_task_orphaned(payload: ActionPayload) -> None:
    """Complete every open reviewing task on the orphaned reviewer ticket (#998).

    The scanner emits this signal when a reviewer-role ticket has a
    non-terminal reviewing task whose URL is no longer in the ``state=opened``
    API response — the underlying MR was merged or closed externally before
    the slot processed the task. Without this sweep the PENDING task lingers
    forever, surfacing on every ``pending-spawn`` and dispatching a reviewer
    sub-agent for nothing.

    The handler is intentionally narrow: it operates by ticket id and only
    completes tasks in ``phase=reviewing`` with non-terminal status. Other
    tasks on the same ticket (or other phases) are untouched. Best-effort —
    a missing ticket or already-completed tasks no-op silently.
    """
    from django.apps import apps  # noqa: PLC0415

    from teatree.core.models.task import Task  # noqa: PLC0415

    ticket_model = apps.get_model("core", "Ticket")
    ticket_id = payload.get("ticket_id")
    if ticket_id is None:
        return
    try:
        ticket = ticket_model.objects.get(pk=ticket_id)
    except ticket_model.DoesNotExist:
        return
    open_tasks = Task.objects.pending_in_phase("reviewing").filter(ticket=ticket)
    completed = 0
    for task in open_tasks:
        task.complete()
        completed += 1
    if completed:
        logger.info(
            "Auto-completed %d orphaned reviewing task(s) on ticket %s (MR %s no longer open)",
            completed,
            ticket_id,
            payload.get("url", "?"),
        )


HANDLERS: dict[str, Callable[[ActionPayload], None]] = {
    "ticket_disposition": ignore_disposed_ticket,
    "ticket_completion": complete_ticket,
    "ticket_reopen": reopen_ticket,
    "reviewer_task_orphaned": reviewer_task_orphaned,
}
