"""Durable ticket-level halt that expires when its inputs change."""

import logging

from teatree.core.models import Task, Ticket

logger = logging.getLogger(__name__)


def _newest_task_pk(ticket: Ticket) -> int:
    return Task.objects.filter(ticket=ticket).order_by("-pk").values_list("pk", flat=True).first() or 0


def ticket_halt_is_current(ticket: Ticket) -> bool:
    stamp = (ticket.extra or {}).get("stuck_halt")
    return (
        isinstance(stamp, dict)
        and stamp.get("newest_task") == _newest_task_pk(ticket)
        and stamp.get("reviewed_sha") == (ticket.extra or {}).get("reviewed_sha", "")
    )


def record_ticket_halt(ticket: Ticket, *, reason: str) -> None:
    ticket.merge_extra(
        set_keys={
            "stuck_halt": {
                "newest_task": _newest_task_pk(ticket),
                "reviewed_sha": str((ticket.extra or {}).get("reviewed_sha", "")),
                "reason": reason,
            }
        }
    )
    logger.warning("Stuck ticket %s stopped redispatching: %s", ticket.pk, reason)
