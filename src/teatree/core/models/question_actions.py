"""Dedupe and answer helpers for durable owner action questions."""

from typing import TYPE_CHECKING, Self

from django.db import models

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
