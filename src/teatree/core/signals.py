import logging

from django.db.models.signals import post_save
from django_fsm.signals import post_transition

from teatree.core.models.pull_request import PullRequest
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.core.on_behalf_gate_recorded import OnBehalfPostBlockedError, require_on_behalf_approval
from teatree.core.on_behalf_post_receipt import notify_user_on_behalf_post
from teatree.core.reaction_dispatch import get_reaction_publisher

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

    try:
        session = instance.sessions.order_by("-started_at").first()  # ty: ignore[unresolved-attribute]
        TicketTransition.objects.create(
            ticket=instance,
            session=session,
            from_state=source,
            to_state=target,
            triggered_by=name,
        )
    except Exception:
        logger.exception("Failed to record TicketTransition audit for ticket %s transition %s", instance.pk, name)


def _add_slack_reactions_on_transition(
    instance: Ticket,
    name: str,
    **_kwargs: object,
) -> None:
    """Post a Slack emoji reaction on the PR review message for this transition.

    The reaction is on a colleague-facing surface (the review-request
    message the user posted to reviewers), so it is gated by the
    recorded-approval on-behalf path. Gate ON + recorded approval scoped
    to ``(ticket.url, "transition_reaction:<name>")`` → reaction posts;
    gate ON + no approval → skip without posting; gate OFF → post. The
    FSM transition itself is never blocked.
    """
    target = f"ticket:{instance.pk}"
    try:
        reacted = require_on_behalf_approval(
            target=target,
            action=f"transition_reaction:{name}",
            publish=lambda: get_reaction_publisher().add_reactions_for_transition(instance, name),
        )
    except OnBehalfPostBlockedError as blocked:
        logger.info("Transition reaction for ticket %s (%s) gated: %s", instance.pk, name, blocked)
        return
    except Exception:
        # A failed react rolled back the consume+audit (#1879 atomicity) — the
        # approval survives for a retry; the FSM transition must never block.
        logger.exception("Failed to add Slack reactions for ticket %s transition %s", instance.pk, name)
        return
    if reacted:
        notify_user_on_behalf_post(
            target=target,
            action=f"transition_reaction:{name}",
            destination=f"ticket:{instance.pk} review message",
            artifact_url=instance.issue_url or target,
            summary=f"{name} transition reaction on ticket {instance.pk}",
        )


def _approval_reaction_target(pull_request: PullRequest) -> str:
    """Stable on-behalf-gate target identifier for the ``approve`` reaction.

    The recorded :class:`OnBehalfApproval` must scope to *this* specific PR
    so an approval for PR A never satisfies the reaction on PR B. The PR's
    ``url`` is the natural unique identifier and is what the user will
    type when recording the approval.
    """
    return pull_request.url or f"{pull_request.repo}#{pull_request.iid}"


def _add_approval_reaction_on_transition(
    instance: PullRequest,
    name: str,
    **_kwargs: object,
) -> None:
    """Post a ✅ on the requester's review message when a PR is approved (#961).

    The reaction is itself a post on the user's behalf, so it routes
    through the same recorded-approval gate every other on-behalf post
    uses (``require_on_behalf_approval`` — gate ON + recorded approval →
    proceed + audit; gate ON + no approval → skip without posting; gate
    OFF → proceed). Satisfiable without a TTY: the user records an
    :class:`OnBehalfApproval` scoped to (PR url, ``approval_reaction``)
    and the next approve transition publishes. The FSM transition itself
    is never blocked — only the on-behalf post is.
    """
    if name != "approve":
        return
    target = _approval_reaction_target(instance)
    try:
        reacted = require_on_behalf_approval(
            target=target,
            action="approval_reaction",
            publish=lambda: get_reaction_publisher().add_approval_reaction(instance),
        )
    except OnBehalfPostBlockedError as blocked:
        logger.info("Approval reaction for PR %s gated: %s", instance.pk, blocked)
        reacted = 0
    except Exception:
        # A failed react rolled back the consume+audit (#1879 atomicity); the
        # approval survives. Continue to the ReviewAssignment bookkeeping —
        # the FSM transition must never block on a reaction failure.
        logger.exception("Failed to add approval reaction for PR %s", instance.pk)
        reacted = 0
    if reacted:
        notify_user_on_behalf_post(
            target=target,
            action="approval_reaction",
            destination=f"PR {instance.url} review message",
            artifact_url=instance.url or target,
            summary=f"✅ approval reaction on PR {instance.url}",
        )
    # #1047: close the reaction-driven loop — mark every ReviewAssignment row
    # for this MR as ``approved`` so the audit trail captures the full
    # reaction → review → approve cycle. Best-effort: a missing row or DB
    # outage must never block the FSM transition.
    try:
        from teatree.core.models import ReviewAssignment  # noqa: PLC0415

        ReviewAssignment.approve_for_mr(mr_url=instance.url, overlay=instance.overlay)
    except Exception:
        logger.exception("Failed to mark ReviewAssignment approved for PR %s", instance.pk)


def _is_loop_dispatched(instance: Task) -> bool:
    """True when the loop is the SOLE dispatcher for this task's ``(role, phase)``.

    A task whose ``(ticket.role, phase)`` has a registered phase agent is
    dispatched per-phase by the in-session ``/loop`` slot (``loop_dispatch``
    ``claim-next`` → the phase sub-agent via the ``Agent`` tool). Such a task
    now defaults to INTERACTIVE at creation (``Task.save`` chokepoint), so the
    ``execution_target`` guard below already skips it; this remains as
    defense-in-depth for a row that reaches HEADLESS some other way, so a
    queue drainer never shells a metered ``claude -p`` for loop phase work. A
    pair with NO registered agent is free-form headless and still rides the
    ``execute_headless_task`` path — never zero dispatch.
    """
    return Task.loop_dispatched(role=instance.ticket.role, phase=instance.phase)


def _auto_enqueue_headless_task(
    sender: type,  # noqa: ARG001
    instance: Task,
    **_kwargs: object,
) -> None:
    """Auto-enqueue HEADLESS tasks for execution when created or re-routed.

    Loop-dispatched phase tasks (those with a registered phase agent) default
    to INTERACTIVE at creation and so fail the ``execution_target`` guard;
    ``_is_loop_dispatched`` stays as a belt-and-braces skip for any HEADLESS
    row of such a pair, so a ``db_worker`` draining the queue never
    double-runs (or meters) loop phase work.

    A task whose ticket names a non-empty unknown overlay is never enqueued
    (souliane/teatree#1959): dispatching it would crash ``execute_headless_task``
    — the drain safety-net fails such rows permanently instead. A blank overlay
    is the ambient single-overlay default and stays dispatchable.
    """
    if instance.execution_target != Task.ExecutionTarget.HEADLESS:
        return
    if instance.status != Task.Status.PENDING:
        return
    if _is_loop_dispatched(instance):
        return
    if not instance.ticket.has_dispatchable_overlay():
        logger.warning("Skipping auto-enqueue of task %s: unknown overlay %r", instance.pk, instance.ticket.overlay)
        return
    from teatree.core.tasks import execute_headless_task  # noqa: PLC0415

    try:
        execute_headless_task.enqueue(int(instance.pk), instance.phase)
        logger.info("Auto-enqueued headless task %s (phase=%s)", instance.pk, instance.phase)
    except Exception:
        logger.exception("Failed to auto-enqueue headless task %s", instance.pk)


def register_signals() -> None:
    post_transition.connect(_log_ticket_transition, sender=Ticket, dispatch_uid="ticket_transition_audit")
    post_transition.connect(
        _add_slack_reactions_on_transition,
        sender=Ticket,
        dispatch_uid="ticket_transition_slack_reactions",
    )
    post_transition.connect(
        _add_approval_reaction_on_transition,
        sender=PullRequest,
        dispatch_uid="pull_request_approval_reaction",
    )
    post_save.connect(_auto_enqueue_headless_task, sender=Task, dispatch_uid="auto_enqueue_headless")
