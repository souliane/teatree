"""Persist agent-kind dispatch actions as Ticket + Task DB rows.

The statusline is for *displaying*; the DB is for *orchestrating*. When
the tick produces a ``DispatchAction(kind="agent", …)`` — a reviewer
request, an auto-start orchestrator, etc. — this module translates the
action into the appropriate ``Ticket`` and initial ``Task`` rows. The
``/loop`` slot then reads pending Tasks via the loop CLI and spawns
sub-agents in-session.

Idempotency lives here, not at the scanner layer: scanners may emit on
every tick (the ``ReviewerPrsScanner`` cache only updates when the
review task actually completes), but a duplicate enqueue is a no-op
because we look up the existing Ticket+Task before creating new rows.
"""

import logging

from teatree.core.models import Task, Ticket
from teatree.loop.dispatch import DispatchAction

logger = logging.getLogger(__name__)

_OPEN_TASK_STATUSES: frozenset[str] = frozenset({Task.Status.PENDING, Task.Status.CLAIMED})


def persist_agent_actions(actions: list[DispatchAction]) -> list[Task]:
    """Translate ``kind="agent"`` actions into DB rows; return the newly created Tasks.

    Each action is dispatched by ``zone`` to a per-zone handler. Unknown
    zones are logged and skipped — the caller (tick) treats this as
    advisory, not fatal.
    """
    created: list[Task] = []
    for action in actions:
        if action.kind != "agent":
            continue
        handler = _ZONE_HANDLERS.get(action.zone)
        if handler is None:
            logger.debug("No persistence handler for agent zone %r", action.zone)
            continue
        task = handler(action)
        if task is not None:
            created.append(task)
    return created


def _handle_reviewer(action: DispatchAction) -> Task | None:
    """Reviewer-requested PR → Ticket(role=reviewer) + Task(phase=reviewing)."""
    payload = action.payload
    pr_url = str(payload.get("url") or "")
    if not pr_url:
        logger.debug("Skipping t3:reviewer action with no url: %r", action.detail)
        return None
    head_sha = str(payload.get("head_sha") or "")
    overlay = str(payload.get("overlay") or "")
    ticket, _ = Ticket.objects.get_or_create(
        issue_url=pr_url,
        defaults={
            "overlay": overlay,
            "role": Ticket.Role.REVIEWER,
            "extra": {"reviewed_sha": head_sha} if head_sha else {},
        },
    )
    if ticket.role != Ticket.Role.REVIEWER:
        logger.debug(
            "Ticket %s exists with role=%s, not promoting to reviewer for PR %s",
            ticket.pk,
            ticket.role,
            pr_url,
        )
        return None
    if head_sha:
        extra = dict(ticket.extra or {})
        if extra.get("reviewed_sha") != head_sha:
            extra["reviewed_sha"] = head_sha
            ticket.extra = extra
            ticket.save(update_fields=["extra"])
    if _has_open_task(ticket, phase="reviewing"):
        return None
    from teatree.core.models.ticket import schedule_external_review  # noqa: PLC0415

    return schedule_external_review(ticket)


def _handle_orchestrator(action: DispatchAction) -> Task | None:
    """Auto-start assigned issue → Ticket(role=author) + Task(phase=coding).

    Only fires for ``assigned_issue.ready`` signals that carry
    ``auto_start=True`` (the dispatcher already filtered). ``pending_task``
    signals — which also resolve to ``t3:orchestrator`` — describe a Task
    that already exists, so we skip them here.
    """
    payload = action.payload
    if payload.get("auto_start") is not True:
        return None
    issue_url = str(payload.get("issue_url") or payload.get("url") or "")
    if not issue_url:
        logger.debug("Skipping t3:orchestrator action with no issue_url: %r", action.detail)
        return None
    overlay = str(payload.get("overlay") or "")
    ticket, _ = Ticket.objects.get_or_create(
        issue_url=issue_url,
        defaults={"overlay": overlay, "role": Ticket.Role.AUTHOR},
    )
    if ticket.role != Ticket.Role.AUTHOR:
        logger.debug(
            "Ticket %s for %s has role=%s; not scheduling coding",
            ticket.pk,
            issue_url,
            ticket.role,
        )
        return None
    if _has_open_task(ticket, phase="coding") or ticket.state != Ticket.State.NOT_STARTED:
        return None
    return ticket.schedule_coding()


def _has_open_task(ticket: Ticket, *, phase: str) -> bool:
    return ticket.tasks.filter(phase=phase, status__in=_OPEN_TASK_STATUSES).exists()  # ty: ignore[unresolved-attribute]


_ZONE_HANDLERS = {
    "t3:reviewer": _handle_reviewer,
    "t3:orchestrator": _handle_orchestrator,
}


__all__ = ["persist_agent_actions"]
