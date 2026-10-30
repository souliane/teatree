"""Failed newest tasks whose tickets either need a successor or are explicitly parked."""

import hashlib
import logging
from collections import Counter
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Exists, OuterRef, Subquery
from django.utils import timezone

from teatree.core.factory.health_signal import HealthSignal, SignalCollection
from teatree.core.forge_url import is_synthetic_ticket_url
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard
from teatree.core.modelkit.task_failure_taxonomy import HALT_STAMP, FailureKind
from teatree.core.models import Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionError
from teatree.core.models.known_issue import KnownIssue

AGED_PARK = timedelta(days=14)
PARKED_MARKER_PREFIX = "parked-tickets:"
_QUESTION = "Should I retire tickets whose failed newest tasks were created over two weeks ago?"
logger = logging.getLogger(__name__)


def failed_newest_tasks() -> list[Task]:
    latest = Task.objects.filter(ticket_id=OuterRef("ticket_id")).order_by("-pk").values("pk")[:1]
    active = Task.objects.filter(ticket_id=OuterRef("ticket_id"), status__in=Task.Status.active())
    terminal = Ticket.marker_release_states() | {Ticket.State.PR_OPENED, Ticket.State.RETRO_RECORDED}
    candidates = (
        Task.objects.filter(status=Task.Status.FAILED, pk=Subquery(latest))
        .exclude(ticket__state__in=terminal)
        .exclude(Exists(active))
        .select_related("ticket")
        .order_by("pk")
    )
    return [task for task in candidates if not is_synthetic_ticket_url(task.ticket.issue_url)]


def parked_failed_tasks() -> list[Task]:
    return [
        task
        for task in failed_newest_tasks()
        if HALT_STAMP in task.execution_reason or task.failure_kind == FailureKind.CANCELLED
    ]


def silent_freeze_tasks() -> list[Task]:
    return [
        task
        for task in failed_newest_tasks()
        if HALT_STAMP not in task.execution_reason and task.failure_kind != FailureKind.CANCELLED
    ]


def aged_parked_tasks(now: datetime | None = None) -> list[Task]:
    cutoff = (now or timezone.now()) - AGED_PARK
    return [task for task in parked_failed_tasks() if task.created_at is not None and task.created_at < cutoff]


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


def _pairs(tasks: list[Task]) -> list[list[int]]:
    return [[task.ticket_id, task.pk] for task in tasks]


def ask_owner_about_parked(tasks: list[Task]) -> DeferredQuestion | None:
    if not tasks:
        return None
    pairs = _pairs(tasks)
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
                "Retire them",
                "I close these work records so any wanted issue can be picked up again.",
                recommended=True,
            ),
            CardOption("Keep them parked", "I leave these work records parked."),
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


def _still_parked(ticket: Ticket, expected_task_id: int) -> bool:
    terminal = Ticket.marker_release_states() | {Ticket.State.PR_OPENED, Ticket.State.RETRO_RECORDED}
    if ticket.state in terminal or is_synthetic_ticket_url(ticket.issue_url):
        return False
    newest = Task.objects.filter(ticket_id=ticket.pk).order_by("-pk").first()
    if newest is None or newest.pk != expected_task_id or newest.status != Task.Status.FAILED:
        return False
    if newest.created_at is None or newest.created_at >= timezone.now() - AGED_PARK:
        return False
    if HALT_STAMP not in newest.execution_reason and newest.failure_kind != FailureKind.CANCELLED:
        return False
    return not Task.objects.filter(ticket_id=ticket.pk, status__in=Task.Status.active()).exists()


def retire_answered_parked(question: DeferredQuestion) -> int:
    if (
        not question.answered_on_owner_channel
        or question.resolved_via != DeferredQuestion.ResolvedVia.SLACK
        or question.answer_text != "Retire them"
    ):
        return 0
    original = {tuple(pair) for pair in question.evidence.get("private_context", {}).get("task_pairs", [])}
    count = 0
    for task in aged_parked_tasks():
        if (task.ticket_id, task.pk) not in original:
            continue
        with transaction.atomic():
            ticket = Ticket.objects.select_for_update().get(pk=task.ticket_id)
            if not _still_parked(ticket, task.pk):
                continue
            ticket.ignore()
            ticket.save(update_fields=["state", "extra"])
            count += 1
    return count
