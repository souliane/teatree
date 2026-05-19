import logging

from django.db.models.signals import post_save
from django_fsm.signals import post_transition

from teatree.backends.slack_reactions import add_approval_reaction, add_reactions_for_transition
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.core.on_behalf_gate_recorded import OnBehalfPostBlockedError, require_on_behalf_approval

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

    session = instance.sessions.order_by("-started_at").first()  # ty: ignore[unresolved-attribute]
    TicketTransition.objects.create(
        ticket=instance,
        session=session,
        from_state=source,
        to_state=target,
        triggered_by=name,
    )


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
        require_on_behalf_approval(target=target, action=f"transition_reaction:{name}")
    except OnBehalfPostBlockedError as blocked:
        logger.info("Transition reaction for ticket %s (%s) gated: %s", instance.pk, name, blocked)
        return
    try:
        add_reactions_for_transition(instance, name)
    except Exception:
        logger.exception("Failed to add Slack reactions for ticket %s transition %s", instance.pk, name)


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
        require_on_behalf_approval(target=target, action="approval_reaction")
    except OnBehalfPostBlockedError as blocked:
        logger.info("Approval reaction for PR %s gated: %s", instance.pk, blocked)
        return
    try:
        add_approval_reaction(instance)
    except Exception:
        logger.exception("Failed to add approval reaction for PR %s", instance.pk)
    # #1047: close the reaction-driven loop — mark every ReviewAssignment row
    # for this MR as ``approved`` so the audit trail captures the full
    # reaction → review → approve cycle. Best-effort: a missing row or DB
    # outage must never block the FSM transition.
    try:
        from teatree.core.models import ReviewAssignment  # noqa: PLC0415

        ReviewAssignment.approve_for_mr(mr_url=instance.url, overlay=instance.overlay)
    except Exception:
        logger.exception("Failed to mark ReviewAssignment approved for PR %s", instance.pk)


def _auto_enqueue_headless_task(
    sender: type,  # noqa: ARG001
    instance: Task,
    **_kwargs: object,
) -> None:
    """Auto-enqueue headless tasks for execution when created or re-routed."""
    if instance.execution_target != Task.ExecutionTarget.HEADLESS:
        return
    if instance.status != Task.Status.PENDING:
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
