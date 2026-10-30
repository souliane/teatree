"""Failed newest tasks whose tickets either need a successor or are explicitly parked."""

import hashlib
import logging
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Exists, OuterRef, Q, QuerySet, Subquery
from django.utils import timezone

from teatree.core.factory.health_signal import HealthSignal, SignalCollection
from teatree.core.forge_url import is_synthetic_ticket_url
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard
from teatree.core.modelkit.task_failure_taxonomy import HALT_STAMP, FailureKind
from teatree.core.models import PullRequest, Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionError
from teatree.core.models.known_issue import KnownIssue

AGED_PARK = timedelta(days=14)
PARKED_MARKER_PREFIX = "parked-tickets:"
RETIRE_ANSWER = "Retire them"
KEEP_ANSWER = "Keep them parked"
_QUESTION = "Should I retire tickets whose failed newest tasks were created over two weeks ago?"
_PARKED = Q(execution_reason__contains=HALT_STAMP) | Q(failure_kind=FailureKind.CANCELLED)
logger = logging.getLogger(__name__)

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


def _deliverable(tasks: Iterable[Task]) -> list[Task]:
    return [task for task in tasks if not is_synthetic_ticket_url(task.ticket.issue_url)]


def parked_failed_tasks() -> list[Task]:
    return _deliverable(_failed_newest_tasks().filter(_PARKED))


def silent_freeze_tasks() -> list[Task]:
    return _deliverable(_failed_newest_tasks().exclude(_PARKED))


def aged_parked_tasks(now: datetime | None = None) -> list[Task]:
    return _deliverable(_aged_parked_queryset(now))


def parked_ticket_signals() -> SignalCollection:
    count = len(aged_parked_tasks())
    if not count:
        return SignalCollection()
    return SignalCollection(
        (
            HealthSignal(
                fingerprint="parked-tickets-await-owner",
                severity=KnownIssue.Severity.WARNING,
                kind="parked_tickets",
                summary=f"{count} tickets have failed newest tasks created over 14 days ago",
            ),
        )
    )


def task_pair(task: Task) -> TaskPair:
    return (task.ticket_id, task.pk)


def ask_owner_about_parked(tasks: list[Task]) -> DeferredQuestion | None:
    if not tasks:
        return None
    pairs = [list(task_pair(task)) for task in tasks]
    digest = hashlib.sha256(repr(pairs).encode()).hexdigest()[:40]
    causes = Counter((task.failure_kind or "unclassified").replace("_", " ") for task in tasks)
    cause_text = ", ".join(f"{count} {cause}" for cause, count in causes.most_common(3))
    card = QuestionCard(
        decision=OwnerDecision.IRREVERSIBLE,
        checked=(
            f"{len(tasks)} tickets have a failed newest task created over two weeks ago.",
            f"Recorded causes include {cause_text}.",
        ),
        blocker="Retiring a ticket ends its current work record.",
        why="The stopped tickets need your decision before they can leave the parked queue.",
        options=(
            CardOption(
                RETIRE_ANSWER,
                "I close these work records so any wanted issue can be picked up again.",
                recommended=True,
            ),
            CardOption(KEEP_ANSWER, "I leave these work records parked."),
        ),
    )
    try:
        return DeferredQuestion.record(
            _QUESTION,
            dedupe_marker=f"{PARKED_MARKER_PREFIX}{digest}",
            card=card,
            private_context={"task_pairs": pairs},
        )
    except DeferredQuestionError:
        logger.exception("parked-ticket owner card refused")
        return None


def owner_ruled_pairs(question: DeferredQuestion, answer: str) -> set[TaskPair]:
    """The tasks an owner-channel *answer* ruled on, or none when the question was answered otherwise."""
    if (
        not question.answered_on_owner_channel
        or question.resolved_via != DeferredQuestion.ResolvedVia.SLACK
        or question.answer_text != answer
    ):
        return set()
    listed = question.evidence.get("private_context", {}).get("task_pairs", [])
    return {(ticket_id, task_id) for ticket_id, task_id in listed}


def _still_parked(ticket: Ticket, expected_task_id: int) -> bool:
    return (
        not is_synthetic_ticket_url(ticket.issue_url)
        and _aged_parked_queryset().filter(pk=expected_task_id, ticket_id=ticket.pk).exists()
    )


def retire_parked(pairs: set[TaskPair], aged: list[Task]) -> int:
    count = 0
    for task in aged:
        if task_pair(task) not in pairs:
            continue
        with transaction.atomic():
            ticket = Ticket.objects.select_for_update().get(pk=task.ticket_id)
            if not _still_parked(ticket, task.pk):
                continue
            ticket.ignore()
            ticket.save(update_fields=["state", "extra"])
            count += 1
    return count
