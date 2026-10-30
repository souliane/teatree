"""Dedupe and answer helpers for durable owner action questions."""

from collections.abc import Collection
from typing import TYPE_CHECKING, Self

from django.db import models
from django.db.models import Value
from django.db.models.functions import Concat

from teatree.core.modelkit.owner_action import read_action
from teatree.core.modelkit.owner_decision import HELD_PR_MARKER_PREFIX, owner_kept_decision
from teatree.core.modelkit.question_card import QuestionCard, match_action_option
from teatree.core.models.question_subject import finished_subject_reason
from teatree.core.models.question_text import question_fingerprint

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit

ACTION_OUTCOMES = ("carried_out", "declined", "refused")
RELEASED_MARKER_PREFIX = "released:"


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
    def answered_without_deciding(self, answers: Collection[str]) -> Self:
        """Answered rows whose answer is not one of *answers* given in Slack: a reply that decided nothing."""
        return self.filter(answered_at__isnull=False).exclude(
            resolved_via=self.model.ResolvedVia.SLACK, answer_text__in=answers
        )

    def release_markers(self) -> int:
        """Free these rows' dedupe markers so the signal they covered is asked again as a fresh question."""
        return self.update(dedupe_marker=Concat(Value(RELEASED_MARKER_PREFIX), "dedupe_marker"))

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
