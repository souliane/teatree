"""Mechanical action handlers — inline ticket transitions executed during a tick.

Each handler receives an ``ActionPayload`` dict and mutates the DB directly.
Called by ``tick._execute_mechanical`` after dispatch, before statusline render.
"""

import logging
from collections.abc import Callable

from django_fsm import can_proceed

from teatree.loop.dispatch import ActionPayload

logger = logging.getLogger(__name__)


def ignore_disposed_ticket(payload: ActionPayload) -> None:
    from django.apps import apps  # noqa: PLC0415

    ticket_model = apps.get_model("core", "Ticket")
    ticket_id = payload.get("ticket_id")
    if ticket_id is None:
        return
    ticket = ticket_model.objects.get(pk=ticket_id)
    # #1087: the disposition signal re-emits every tick while the ticket
    # stays IGNORED (its PR keystone-merged, issue auto-closed). Driving
    # ``ignore`` from ``ignored`` is not a valid FSM transition — guard so
    # the already-satisfied desired state is a silent no-op, not every-tick
    # ``TransitionNotAllowed`` noise.
    if not can_proceed(ticket.ignore):
        return
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
    # #1087: same re-emit hazard as ``ignore_disposed_ticket`` — a reopen
    # signal that persists across ticks would drive ``reopen`` from the
    # already-STARTED target state, raising every-tick ``TransitionNotAllowed``.
    if not can_proceed(ticket.reopen):
        return
    ticket.reopen()
    ticket.save()
    logger.info("Auto-reopened ticket %s (was %s, draft MRs detected)", ticket_id, payload.get("ticket_state", "?"))


def reviewer_task_orphaned(payload: ActionPayload) -> None:
    """Complete every open reviewing task on the orphaned reviewer ticket (#998).

    The scanner emits this signal ONLY after ``host.get_pr_open_state``
    confirmed the PR is genuinely MERGED or CLOSED (#1074) — never on mere
    absence from the reviewer-assignment scan. Without this sweep the
    PENDING task for a truly-merged PR lingers forever, surfacing on every
    ``pending-spawn`` and dispatching a reviewer sub-agent for nothing.

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
            "Auto-completed %d orphaned reviewing task(s) on ticket %s (PR %s confirmed merged/closed)",
            completed,
            ticket_id,
            payload.get("url", "?"),
        )


def assign_gitlab_reviewer(payload: ActionPayload) -> None:
    """Append the user as reviewer on the MR carried by *payload* (#1295 cap B).

    Reads ``url`` and ``reviewer_username`` from the payload, resolves
    the active overlay's GitLab host, and calls
    :meth:`GitLabCodeHost.assign_reviewer` which preserves the existing
    reviewer list. Best-effort: any failure logs without raising so a
    Slack mention on a non-GitLab forge or a transient API hiccup
    cannot wedge the tick.
    """
    pr_url = str(payload.get("url") or payload.get("mr_url") or "")
    reviewer_username = str(payload.get("reviewer_username", ""))
    if not pr_url or not reviewer_username:
        return
    try:
        from teatree.backends.loader import get_code_host  # noqa: PLC0415
        from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

        overlay = get_overlay()
        host = get_code_host(overlay)
    except Exception:
        logger.exception("Could not resolve code host for cap-B assignment of %s", pr_url)
        return
    if host is None:
        logger.info("No code host resolved for cap-B assignment of %s", pr_url)
        return
    assign = getattr(host, "assign_reviewer", None)
    if assign is None or not callable(assign):
        logger.info("Code host has no assign_reviewer support for %s — skipping cap-B", pr_url)
        return
    try:
        ok = assign(pr_url=pr_url, username=reviewer_username)
    except Exception:
        logger.exception("Failed to assign %s as reviewer on %s", reviewer_username, pr_url)
        return
    if ok:
        logger.info("Assigned %s as reviewer on %s via Slack-mention pickup", reviewer_username, pr_url)
    else:
        logger.warning("assign_reviewer returned False for %s on %s", reviewer_username, pr_url)


HANDLERS: dict[str, Callable[[ActionPayload], None]] = {
    "ticket_disposition": ignore_disposed_ticket,
    "ticket_completion": complete_ticket,
    "ticket_reopen": reopen_ticket,
    "reviewer_task_orphaned": reviewer_task_orphaned,
    "assign_gitlab_reviewer": assign_gitlab_reviewer,
}
