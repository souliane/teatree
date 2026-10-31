"""A running task's escalation: one internal request row, who answers it, and what its deadline does (#5685).

The request is information for the coordinator. It never acts for the asker, so it cannot spend money,
delete unremoted work, widen a credential's use or speak to a colleague. The only owner-facing outcome
is an ordinary question card, raised after the deadline and only for a kind the owner may be asked.
"""

from datetime import datetime, timedelta
from hashlib import sha256
from typing import Final

from django.db import models, transaction
from django.utils import timezone

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.phases import ANSWERING_PHASES
from teatree.core.modelkit.question_card import QuestionCard
from teatree.core.models import Task
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.task_handoff import CARD_DISCUSSION_PREFIX, schedule_card_discussion

ESCALATION_DEADLINE: Final = timedelta(minutes=30)
INTERNAL_KIND: Final = "internal"
KINDS: Final = (INTERNAL_KIND, *OwnerDecision)
MARKER_PREFIX: Final = "esc:"
UNSERVED_PREFIX: Final = "escalation_unserved: "
_OWNER_MARKER_PREFIX: Final = "esc-owner:"
_RESOLVER: Final = "escalation_deadline"
_ANSWERED_RESOLVER: Final = "coordinator_reply"
_KIND_SUBJECT: Final = {
    OwnerDecision.CREDENTIALS: "who may use a credential",
    OwnerDecision.MONEY_OR_PLAN: "spending or a plan",
    OwnerDecision.PUBLIC_POST: "a public post",
    OwnerDecision.IRREVERSIBLE: "a step that cannot be undone",
    OwnerDecision.ARCHITECTURE: "an architecture choice",
}


class EscalationRefusedError(ValueError):
    """The request was refused before anything was written; the message says what to change."""


def _marker(task: Task, kind: str, message: str) -> str:
    identity = "\0".join((str(task.pk), kind, message))
    return f"{MARKER_PREFIX}{sha256(identity.encode()).hexdigest()[:32]}"


def record_request(task: Task, *, kind: str, message: str) -> DeferredQuestion:
    """The one pending internal row for this exact request; a repeat of it returns the same row."""
    if kind not in KINDS:
        valid = " | ".join(KINDS)
        msg = f"invalid kind {kind!r} — valid kinds: {valid}"
        raise EscalationRefusedError(msg)
    text = message.strip()
    if not text:
        msg = "an escalation needs a message"
        raise EscalationRefusedError(msg)
    return DeferredQuestion.record(
        text,
        parked_task=task,
        task_session=task.session,
        dedupe_marker=_marker(task, kind, text),
        evidence={"escalation": {"kind": kind, "task": task.pk}},
    )


def _requests() -> models.QuerySet[DeferredQuestion]:
    return DeferredQuestion.internal_pending().filter(dedupe_marker__startswith=MARKER_PREFIX)


def _deadline_cutoff() -> datetime:
    return timezone.now() - ESCALATION_DEADLINE


def open_request(ticket_id: int) -> DeferredQuestion | None:
    """The newest request of *ticket_id* that is still waiting and inside its deadline."""
    open_ones = _requests().filter(parked_task__ticket_id=ticket_id, created_at__gt=_deadline_cutoff())
    return open_ones.order_by("-created_at").first()


def answerable_request(ticket_id: int) -> DeferredQuestion | None:
    """The open request of *ticket_id* when its asker is the one task running there outside the answering phase."""
    if (request := open_request(ticket_id)) is None:
        return None
    running = Task.objects.filter(ticket_id=ticket_id, status=Task.Status.CLAIMED).exclude(phase__in=ANSWERING_PHASES)
    return request if list(running.values_list("pk", flat=True)) == [request.parked_task_id] else None


def close_answered_request(row: DeferredQuestion) -> None:
    row.mark_stale("answered by the coordinator", resolver_id=_ANSWERED_RESOLVER)


def _answering(ticket_id: int) -> models.QuerySet[Task]:
    return Task.objects.filter(ticket_id=ticket_id, phase__in=ANSWERING_PHASES)


def coordinator_task(ticket_id: int) -> Task | None:
    """The answering task that talks for the ticket's thread: the newest one, not a continuation still queued."""
    queued = models.Q(status=Task.Status.PENDING, execution_reason__startswith=CARD_DISCUSSION_PREFIX)
    return _answering(ticket_id).exclude(queued).order_by("-pk").first()


def resume_coordinator(ticket_id: int, *, event_id: str, message: str) -> Task | None:
    """Queue *message* as the first input of the thread's coordinator task, once per *event_id*."""
    marker = f"{CARD_DISCUSSION_PREFIX}{event_id}: "
    if (queued := _answering(ticket_id).filter(execution_reason__contains=marker).first()) is not None:
        return queued
    parent = coordinator_task(ticket_id)
    return None if parent is None else schedule_card_discussion(parent, event_id=event_id, message=message)


def _unserved_card(decision: OwnerDecision, subject: str, asked: str) -> tuple[str, QuestionCard]:
    question = f"A running task needs your decision on {subject} and nobody else answered in time, what should it do?"
    card = QuestionCard(
        decision=decision,
        checked=("The request went to the coordinator and nobody answered it in time.",),
        blocker="The task cannot go on until this is decided.",
        why="The task asked its coordinator first, and nobody answered within the deadline.",
    )
    return question, card.with_quote(question, asked)


def settle_request(row: DeferredQuestion) -> bool:
    """Close *row* now: an owner card or a failed asker; ``False`` when it was no longer pending."""
    with transaction.atomic():
        pending = DeferredQuestion.objects.select_for_update().filter(pk=row.pk).first()
        if pending is None or not pending.is_pending:
            return False
        task = pending.parked_task
        kind = pending.evidence["escalation"]["kind"]
        if task is None or task.status in Task.Status.terminal():
            pending.mark_stale("the asking task finished before the deadline", resolver_id=_RESOLVER)
            return True
        if (subject := _KIND_SUBJECT.get(kind)) is not None:
            question, card = _unserved_card(OwnerDecision(kind), subject, pending.question)
            raised = DeferredQuestion.record(
                question,
                card=card,
                parked_task=task,
                task_session=task.session,
                dedupe_marker=f"{_OWNER_MARKER_PREFIX}{pending.pk}",
            )
            pending.mark_stale(f"raised to the owner as question {raised.pk}", resolver_id=_RESOLVER)
        else:
            task.fail(reason=f"{UNSERVED_PREFIX}no coordinator answered escalation {pending.pk}", by_holder=False)
            pending.mark_stale("no coordinator answered; the asking task was failed", resolver_id=_RESOLVER)
    return True


def settle_expired_escalations() -> int:
    """Close each request past its deadline; the number closed."""
    expired = _requests().filter(created_at__lte=_deadline_cutoff())
    return sum(settle_request(row) for row in list(expired))
