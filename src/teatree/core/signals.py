import logging

from django.db.models.signals import post_save
from django_fsm.signals import post_transition

from teatree.core.models.task import Task
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

    session = instance.sessions.order_by("-started_at").first()  # ty: ignore[unresolved-attribute]
    TicketTransition.objects.create(
        ticket=instance,
        session=session,
        from_state=source,
        to_state=target,
        triggered_by=name,
    )


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
    post_save.connect(_auto_enqueue_headless_task, sender=Task, dispatch_uid="auto_enqueue_headless")
