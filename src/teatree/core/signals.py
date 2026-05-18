import logging

from django.db.models.signals import post_save
from django_fsm.signals import post_transition

from teatree.backends.slack_reactions import add_approval_reaction, add_reactions_for_transition
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.on_behalf_gate import ask_before_post_on_behalf_enabled

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
    """Post a Slack emoji reaction on the PR review message for this transition."""
    try:
        add_reactions_for_transition(instance, name)
    except Exception:
        logger.exception("Failed to add Slack reactions for ticket %s transition %s", instance.pk, name)


def _add_approval_reaction_on_transition(
    instance: PullRequest,
    name: str,
    **_kwargs: object,
) -> None:
    """Post a ✅ on the requester's review message when a PR is approved (#961).

    The approval reaction is itself a post on the user's behalf, so it is
    suppressed while the ``ask_before_post_on_behalf`` pre-gate (#960) is
    on (its default) — the agent asks the user before the reaction goes
    out, exactly as for any other on-behalf post. The user flips the gate
    off per-overlay once they trust the system posts well.
    """
    if name != "approve":
        return
    if ask_before_post_on_behalf_enabled():
        return
    try:
        add_approval_reaction(instance)
    except Exception:
        logger.exception("Failed to add approval reaction for PR %s", instance.pk)


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
