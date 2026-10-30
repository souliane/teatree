"""Failed newest tasks whose tickets either need a successor or are explicitly parked."""

from collections.abc import Iterable
from datetime import datetime, timedelta

from django.db.models import Exists, OuterRef, Q, QuerySet, Subquery
from django.utils import timezone

from teatree.core.forge_url import is_synthetic_ticket_url
from teatree.core.modelkit.task_failure_taxonomy import HALT_STAMP, FailureKind
from teatree.core.models import PullRequest, Task, Ticket

AGED_PARK = timedelta(days=14)
_PARKED = Q(execution_reason__contains=HALT_STAMP) | Q(failure_kind=FailureKind.CANCELLED)

TaskPair = tuple[int, int]


def _failed_newest_tasks() -> QuerySet[Task]:
    latest = Task.objects.filter(ticket_id=OuterRef("ticket_id")).order_by("-pk").values("pk")[:1]
    active = Task.objects.filter(ticket_id=OuterRef("ticket_id"), status__in=Task.Status.active())
    done = Ticket.finished_states() | {Ticket.State.PR_OPENED}
    return (
        Task.objects.filter(status=Task.Status.FAILED, pk=Subquery(latest))
        .exclude(ticket__state__in=done)
        .exclude(Exists(active))
        .select_related("ticket")
        .order_by("pk")
    )


def _aged_parked_queryset(now: datetime | None = None) -> QuerySet[Task]:
    open_pull_request = PullRequest.objects.live().filter(ticket_id=OuterRef("ticket_id"))
    return (
        _failed_newest_tasks()
        .filter(_PARKED, created_at__lt=(now or timezone.now()) - AGED_PARK)
        .exclude(ticket__state=Ticket.State.REVIEW_REQUESTED)
        .exclude(Exists(open_pull_request))
    )


def aged_parked_ticket_ids() -> QuerySet[Task]:
    return _aged_parked_queryset().values("ticket_id")


def _deliverable(tasks: Iterable[Task]) -> list[Task]:
    return [task for task in tasks if not is_synthetic_ticket_url(task.ticket.issue_url)]


def parked_failed_tasks() -> list[Task]:
    return _deliverable(_failed_newest_tasks().filter(_PARKED))


def silent_freeze_tasks() -> list[Task]:
    return _deliverable(_failed_newest_tasks().exclude(_PARKED))


def aged_parked_tasks(now: datetime | None = None) -> list[Task]:
    return _deliverable(_aged_parked_queryset(now))


def still_parked(ticket: Ticket, expected_task_id: int) -> bool:
    return (
        not is_synthetic_ticket_url(ticket.issue_url)
        and _aged_parked_queryset().filter(pk=expected_task_id, ticket_id=ticket.pk).exists()
    )


def task_pair(task: Task) -> TaskPair:
    return (task.ticket_id, task.pk)
