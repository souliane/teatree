"""Reuse an exact owner answer for a repeated architecture question."""

import logging

from django.db import DatabaseError, transaction

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import QuestionCard
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from teatree.core.models.errors import NoPlanArtifactError
from teatree.core.models.question_subject import finished_subject_reason
from teatree.core.models.question_text import question_fingerprint
from teatree.core.models.task_handoff import schedule_resume

logger = logging.getLogger(__name__)


def eligible_for_coordination(row: DeferredQuestion) -> bool:
    card = QuestionCard.from_row(row.evidence, row.options_json) if isinstance(row.evidence, dict) else None
    return bool(
        row.is_pending
        and row.audience == DeferredQuestion.Audience.OWNER_QUESTION
        and card is not None
        and card.decision == OwnerDecision.ARCHITECTURE
        and not card.problems(row.question)
        and finished_subject_reason(row) is None
    )


def matching_owner_basis(row: DeferredQuestion) -> DeferredQuestion | None:
    """Find a prior owner-channel answer with the same kind, options and normalized question."""
    if not eligible_for_coordination(row):
        return None
    fingerprint = question_fingerprint(row.question)
    candidates = (
        DeferredQuestion.objects.filter(
            audience=DeferredQuestion.Audience.OWNER_QUESTION,
            answered_at__isnull=False,
            resolved_via__in=DeferredQuestion.OWNER_CHANNELS,
            evidence__decision=OwnerDecision.ARCHITECTURE.value,
            options_hash=row.options_hash,
        )
        .exclude(pk=row.pk)
        .exclude(answer_text="")
        .order_by("-answered_at", "-pk")
    )
    for basis in candidates.iterator():
        if question_fingerprint(basis.question) == fingerprint and _basis_matches(row, basis):
            return basis
    return None


def _basis_matches(row: DeferredQuestion, basis: DeferredQuestion) -> bool:
    if not basis.answered_on_owner_channel or not basis.answer_text or basis.pk == row.pk:
        return False
    if basis.options_hash != row.options_hash or question_fingerprint(basis.question) != question_fingerprint(
        row.question
    ):
        return False
    if not isinstance(basis.evidence, dict):
        return False
    card = QuestionCard.from_row(basis.evidence, basis.options_json)
    return card is not None and card.decision is OwnerDecision.ARCHITECTURE and not card.problems(basis.question)


def coordinate_answer(row: DeferredQuestion, basis: DeferredQuestion, answer: str) -> DeferredQuestion | None:
    """Consume a pending architecture card with an answer grounded in a matching owner card."""
    if not eligible_for_coordination(row) or basis.answer_text != answer or not _basis_matches(row, basis):
        return None
    with transaction.atomic():
        resolved = row.apply_answer(answer, resolved_via=DeferredQuestion.ResolvedVia.AGENT)
        if resolved is None:
            return None
        DeferredQuestionAudit.objects.create(
            question=resolved,
            action="coordinated",
            answer_text=answer,
            note=f"Owner answer on question {basis.pk}",
            resolver_id="coordinator",
        )
    parked_task = resolved.parked_task
    if parked_task is not None and finished_subject_reason(resolved) is None:
        try:
            schedule_resume(parked_task, answer=answer, question=resolved.question)
        except NoPlanArtifactError:
            logger.warning("Coordinated answer to question %s kept; task was not resumed", resolved.pk)
    return resolved


def coordinate_pending(rows: list[DeferredQuestion]) -> list[DeferredQuestion]:
    """Resolve exact repeats before any caller counts or posts the owner backlog."""
    live: list[DeferredQuestion] = []
    for row in rows:
        try:
            basis = matching_owner_basis(row)
            coordinated = basis is not None and coordinate_answer(row, basis, basis.answer_text) is not None
        except DatabaseError:
            logger.warning("Could not coordinate question %s; owner card remains pending", row.pk, exc_info=True)
            coordinated = False
        if not coordinated:
            live.append(row)
    return live
