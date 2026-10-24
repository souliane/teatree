"""Dedupe and answer helpers for durable owner action questions."""

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Self

from django.db import models, transaction
from django.utils import timezone

from teatree.core.modelkit.owner_action import read_action
from teatree.core.modelkit.question_card import QuestionCard, match_action_option
from teatree.core.models.question_text import question_fingerprint

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit

ACTION_OUTCOMES = ("carried_out", "declined", "refused")
DELEGATION_SKIPPED_ACTION = "delegation_skipped"


def is_action_question(row: "DeferredQuestion") -> bool:
    return read_action(row.evidence) is not None


def owner_declined(row: "DeferredQuestion") -> bool:
    option = match_action_option(row.options_json, row.answer_text)
    return row.resolved_via == row.ResolvedVia.SLACK and option is not None and not option.recommended


def held_question(
    marked: "QuerySet[DeferredQuestion]", question: str, *, card: "QuestionCard | None", options_json: str = ""
) -> "DeferredQuestion | None":
    pending = marked.filter(answered_at__isnull=True, dismissed_at__isnull=True)
    if card is None:
        return pending.first()
    if card.action is not None:
        return _held_action(marked, pending)
    return _held_decision(marked, pending, question, card.to_evidence(), options_json)


def _held_action(
    marked: "QuerySet[DeferredQuestion]", pending: "QuerySet[DeferredQuestion]"
) -> "DeferredQuestion | None":
    if held := pending.filter(audience=marked.model.Audience.OWNER_QUESTION).first():
        return held
    waiting = marked.filter(audience=marked.model.Audience.OWNER_QUESTION, answered_at__isnull=False).exclude(
        audits__action__in=ACTION_OUTCOMES
    )
    if held := next((row for row in waiting if not owner_declined(row)), None):
        return held
    _supersede(pending)
    return None


def _held_decision(
    marked: "QuerySet[DeferredQuestion]",
    pending: "QuerySet[DeferredQuestion]",
    question: str,
    evidence: Mapping[str, object],
    options_json: str,
) -> "DeferredQuestion | None":
    fingerprint = question_fingerprint(question)
    for row in marked.filter(audience=marked.model.Audience.OWNER_QUESTION):
        reworded = row.dismissed_at is not None and question_fingerprint(row.question) != fingerprint
        if not reworded and _facts_unchanged(row, evidence, options_json):
            return row
    _supersede(pending)
    return None


def _supersede(pending: "QuerySet[DeferredQuestion]") -> None:
    for previous in pending:
        previous.mark_stale("superseded by an owner question", resolver_id="record_owner_supersedes")


def _facts_unchanged(row: "DeferredQuestion", evidence: Mapping[str, object], options_json: str) -> bool:
    previous = row.evidence if isinstance(row.evidence, dict) else {}
    return previous == dict(evidence) and row.options_json == options_json


def mark_escalated(row: "DeferredQuestion", note: str, audit_model: type["DeferredQuestionAudit"]) -> bool:
    with transaction.atomic():
        current = (
            type(row)
            .objects.select_for_update()
            .filter(pk=row.pk, answered_at__isnull=True, dismissed_at__isnull=True)
            .first()
        )
        if current is None:
            return False
        current.escalated_at = timezone.now()
        current.escalation_count += 1
        current.save(update_fields=["escalated_at", "escalation_count"])
        audit_model.objects.create(question=current, action="escalated", note=note)
        row.escalated_at = current.escalated_at
        row.escalation_count = current.escalation_count
        return True


class DeferredQuestionQuerySet(models.QuerySet["DeferredQuestion"]):
    def decided_since(self, since: datetime) -> Self:
        decided = models.Q(answered_at__gte=since) | models.Q(dismissed_at__gte=since)
        return self.filter(decided).exclude(resolved_via=self.model.ResolvedVia.STALE)

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

    def awaiting_ledger_application(self) -> Self:
        return (
            self.filter(
                audience=self.model.Audience.OWNER_QUESTION,
                answered_at__isnull=False,
                applied_at__isnull=True,
                parked_task__isnull=True,
                session_id="",
            )
            .exclude(evidence__has_key="action")
            .order_by("answered_at", "pk")
        )


class DeferredQuestionAuditQuerySet(models.QuerySet["DeferredQuestionAudit"]):
    def owner_answer(self, question: "DeferredQuestion") -> "DeferredQuestionAudit | None":
        return (
            self.filter(question=question, action="answered", resolver_id__startswith="slack:").order_by("-pk").first()
        )
