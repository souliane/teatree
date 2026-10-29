"""Dedupe and answer helpers for durable owner action questions."""

from datetime import timedelta
from typing import TYPE_CHECKING, Self

from django.db import models
from django.utils import timezone

from teatree.core.modelkit.owner_action import read_action
from teatree.core.modelkit.owner_decision import HELD_PR_MARKER_PREFIX, owner_kept_decision
from teatree.core.modelkit.question_card import QuestionCard, match_action_option
from teatree.core.models.question_subject import finished_subject_reason
from teatree.core.models.question_text import question_fingerprint

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
    from teatree.core.models.session import Session
    from teatree.core.models.task import Task

ACTION_OUTCOMES = ("carried_out", "declined", "refused")


def is_action_question(row: "DeferredQuestion") -> bool:
    return read_action(row.evidence) is not None


def owner_declined(row: "DeferredQuestion") -> bool:
    option = match_action_option(row.options_json, row.answer_text)
    return row.resolved_via == row.ResolvedVia.SLACK and option is not None and not option.recommended


def dismiss_kept_for_owner(row: "DeferredQuestion", *, direct_owner: bool, dispatched: bool) -> bool:
    if row.audience != row.Audience.OWNER_QUESTION or direct_owner or finished_subject_reason(row):
        return False
    if dispatched:
        return True
    card = QuestionCard.from_row(row.evidence, row.options_json)
    return card is None or row.dedupe_marker.startswith(HELD_PR_MARKER_PREFIX) or owner_kept_decision(card.decision)


def reuse_recent_owner_answer(
    model: type["DeferredQuestion"],
    question: str,
    card: QuestionCard | None,
    options_hash: str,
    subject: str,
) -> "DeferredQuestion | None":
    """Audit and reuse an exact owner decision on the same subject within one day."""
    if card is None or not subject or card.action is not None or card.names_rules_file(question):
        return None
    candidates = (
        model.objects.filter(
            audience=model.Audience.OWNER_QUESTION,
            answered_at__gte=timezone.now() - timedelta(days=1),
            resolved_via__in=model.OWNER_CHANNELS,
            evidence__decision=card.decision.value,
            options_hash=options_hash,
        )
        .exclude(answer_text="")
        .select_related("parked_task__ticket", "task_session__ticket")
        .order_by("-answered_at", "-pk")
    )
    fingerprint = question_fingerprint(question)
    for basis in candidates:
        basis_card = QuestionCard.from_row(basis.evidence, basis.options_json)
        if (
            basis_card == card
            and question_fingerprint(basis.question) == fingerprint
            and decision_subject(basis_card, parked_task=basis.parked_task, task_session=basis.task_session) == subject
        ):
            basis.audits.create(
                action="reused",
                answer_text=basis.answer_text,
                note="Exact owner decision reused at record time",
                resolver_id="record_recent_answer",
            )
            return basis
    return None


def decision_subject(card: QuestionCard, *, parked_task: "Task | None", task_session: "Session | None") -> str:
    if card.subject:
        return card.subject
    if parked_task is not None:
        return parked_task.ticket.issue_url
    if task_session is not None:
        return task_session.ticket.issue_url
    return ""


def held_question(
    marked: "QuerySet[DeferredQuestion]", question: str, *, owner: bool, action: bool
) -> "DeferredQuestion | None":
    pending = marked.filter(answered_at__isnull=True, dismissed_at__isnull=True)
    if not owner:
        return pending.first()
    if action:
        if held := pending.filter(audience=marked.model.Audience.OWNER_QUESTION).first():
            return held
        waiting = marked.filter(audience=marked.model.Audience.OWNER_QUESTION, answered_at__isnull=False).exclude(
            audits__action__in=ACTION_OUTCOMES
        )
        if held := next((row for row in waiting if not owner_declined(row)), None):
            return held
        for internal in pending:
            internal.mark_stale("superseded by an owner question", resolver_id="record_owner_supersedes")
        return None
    fingerprint = question_fingerprint(question)
    for row in marked.filter(audience=marked.model.Audience.OWNER_QUESTION):
        if row.dismissed_at is None or question_fingerprint(row.question) == fingerprint:
            return row
    for internal in pending:
        internal.mark_stale("superseded by an owner question", resolver_id="record_owner_supersedes")
    return None


class DeferredQuestionQuerySet(models.QuerySet["DeferredQuestion"]):
    def awaiting_action_outcome(self) -> Self:
        return (
            self.filter(
                audience=self.model.Audience.OWNER_QUESTION,
                answered_at__isnull=False,
                evidence__has_key="action",
            )
            .exclude(audits__action__in=ACTION_OUTCOMES)
            .order_by("answered_at", "pk")
        )


class DeferredQuestionAuditQuerySet(models.QuerySet["DeferredQuestionAudit"]):
    def owner_answer(self, question: "DeferredQuestion") -> "DeferredQuestionAudit | None":
        return (
            self.filter(question=question, action="answered", resolver_id__startswith="slack:").order_by("-pk").first()
        )
