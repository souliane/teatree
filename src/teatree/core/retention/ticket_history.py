"""Which tickets have gone quiet, so retention may delete their task history.

A ticket is quiescent at *cutoff* when it is finished, is not a synthetic loop ticket,
and nothing about it moved since *cutoff*: no active task, no task created and no attempt
started, no transition of any kind. Two readers of a task row outlive the ticket's
activity and also keep it: a PENDING review broadcast naming one of its tasks as the
reviewer (deleting that task re-emits the review), and an open question parked on one of
its tasks (its answer must resume that task, not the asking session).

Synthetic loop tickets are never quiescent: their root-task count bounds directive
dispatch, so pruning would reset the bound and turn a resumed child into a root.
"""

import datetime as dt

from django.db import models
from django.db.models.functions import Cast

from teatree.core.models import DeferredQuestion, ScannedBroadcast, Task, TaskAttempt, Ticket, TicketTransition
from teatree.utils.url_slug import SYNTHETIC_LOOP_UMBRELLA_URL


def synthetic_loop_umbrella_q(field: str) -> models.Q:
    """The ORM twin of :func:`teatree.utils.url_slug.is_synthetic_loop_umbrella_url` over the column *field*."""
    return models.Q(**{field: SYNTHETIC_LOOP_UMBRELLA_URL}) | models.Q(
        **{f"{field}__startswith": f"{SYNTHETIC_LOOP_UMBRELLA_URL}#"}
    )


def _reviewer_task_tickets() -> models.QuerySet:
    pending = ScannedBroadcast.objects.filter(classification=ScannedBroadcast.Classification.PENDING)
    return (
        Task.objects.annotate(pk_text=Cast("pk", models.CharField()))
        .filter(pk_text__in=pending.values("reviewer_task_id"))
        .values("ticket_id")
    )


def quiescent_tickets(cutoff: dt.datetime) -> models.QuerySet[Ticket]:
    return (
        Ticket.objects.filter(state__in=Ticket.finished_states())
        .exclude(synthetic_loop_umbrella_q("issue_url"))
        .exclude(pk__in=Task.objects.filter(status__in=Task.Status.active()).values("ticket_id"))
        .exclude(pk__in=Task.objects.filter(created_at__gte=cutoff).values("ticket_id"))
        .exclude(pk__in=TaskAttempt.objects.filter(started_at__gte=cutoff).values("task__ticket_id"))
        .exclude(pk__in=TicketTransition.objects.filter(created_at__gte=cutoff).values("ticket_id"))
        .exclude(pk__in=_reviewer_task_tickets())
        .exclude(
            pk__in=DeferredQuestion.pending()
            .filter(parked_task__isnull=False)
            .order_by()
            .values("parked_task__ticket_id")
        )
    )


def prunable_tasks(cutoff: dt.datetime, status: Task.Status) -> models.QuerySet[Task]:
    """The *status* tasks of tickets quiescent at *cutoff*; every such task is terminal by guard."""
    return Task.objects.filter(status=status, ticket__in=quiescent_tickets(cutoff))


__all__ = ["prunable_tasks", "quiescent_tickets", "synthetic_loop_umbrella_q"]
