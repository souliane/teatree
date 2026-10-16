"""Dedupe and answer helpers for durable owner action questions."""

from typing import TYPE_CHECKING

from teatree.core.modelkit.owner_action import read_action
from teatree.core.modelkit.question_card import match_action_option
from teatree.core.models.question_text import question_fingerprint

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit

ACTION_OUTCOMES = ("carried_out", "declined", "refused")


def is_action_question(row: "DeferredQuestion") -> bool:
    return read_action(row.evidence) is not None


def canonical_action_answer(row: "DeferredQuestion", answer: str) -> str | None:
    if not is_action_question(row):
        return answer
    option = match_action_option(row.options_json, answer)
    return option.label if option is not None else None


def held_question(
    marked: "QuerySet[DeferredQuestion]", question: str, *, owner: bool, action: bool
) -> "DeferredQuestion | None":
    pending = marked.filter(answered_at__isnull=True, dismissed_at__isnull=True)
    if not owner:
        return pending.first()
    if action:
        if held := pending.filter(audience=marked.model.Audience.OWNER_QUESTION).first():
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


def action_awaiting_outcome(model: "type[DeferredQuestion]") -> "QuerySet[DeferredQuestion]":
    return (
        model.objects.filter(
            audience=model.Audience.OWNER_QUESTION,
            answered_at__isnull=False,
            evidence__has_key="action",
        )
        .exclude(audits__action__in=ACTION_OUTCOMES)
        .order_by("answered_at", "pk")
    )


def owner_answerer(row: "DeferredQuestion", audit_model: "type[DeferredQuestionAudit]") -> str:
    answer = (
        audit_model.objects.filter(question=row, action="answered", resolver_id__startswith="slack:")
        .order_by("-pk")
        .first()
    )
    return answer.resolver_id if answer is not None else ""
