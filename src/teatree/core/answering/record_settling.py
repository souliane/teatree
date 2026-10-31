"""A question the code can settle is not raised to the owner: a recent owner answer, or the option crossing no limit."""

import dataclasses
import datetime as dt
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from django.db import transaction
from django.utils import timezone

from teatree.core.answering.question_coordinator import matching_owner_basis
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from teatree.core.models.question_text import options_digest, question_fingerprint
from teatree.core.models.session import Session

_ANSWER_REUSE_WINDOW: Final = dt.timedelta(days=1)
_RESOLVER: Final = "limit_table"
_DECISION_MARKER: Final = "limit-choice:"


@dataclasses.dataclass(frozen=True, slots=True)
class LimitChoice:
    crosses: CardOption
    crosses_none: CardOption


LIMIT_CHOICES: Final[Mapping[OwnerDecision, LimitChoice]] = MappingProxyType(
    {
        OwnerDecision.CREDENTIALS: LimitChoice(
            CardOption("Widen it", "I give the credential a wider use than it has today."),
            CardOption("Keep it as it is", "I change no credential and no security setting."),
        ),
        OwnerDecision.MONEY_OR_PLAN: LimitChoice(
            CardOption("Spend it", "I spend the money or take the plan."),
            CardOption("Spend nothing", "I spend no money and take no plan."),
        ),
        OwnerDecision.PUBLIC_POST: LimitChoice(
            CardOption("Say it", "I send the message in your name."),
            CardOption("Say nothing", "I send nothing in your name."),
        ),
        OwnerDecision.IRREVERSIBLE: LimitChoice(
            CardOption("Delete it", "I delete the work for good."),
            CardOption("Keep it", "I delete nothing."),
        ),
    }
)


def _limit_choice(card: QuestionCard) -> CardOption | None:
    """The table's option crossing no limit, when the card offers exactly the table's two options for its decision."""
    choice = LIMIT_CHOICES.get(card.decision)
    offered = frozenset(dataclasses.replace(option, recommended=False) for option in card.options)
    return choice.crosses_none if choice is not None and offered == {choice.crosses, choice.crosses_none} else None


def _recent_owner_answer(question: str, card: QuestionCard, dedupe_marker: str) -> DeferredQuestion | None:
    unrecorded = DeferredQuestion(
        question=question,
        options_hash=options_digest(card.option_dicts()) if card.options else "",
        evidence=card.to_evidence(),
        dedupe_marker=dedupe_marker,
    )
    if (basis := matching_owner_basis(unrecorded)) is None:
        return None
    recent = DeferredQuestion.objects.filter(pk=basis.pk, answered_at__gte=timezone.now() - _ANSWER_REUSE_WINDOW)
    return basis if recent.exists() else None


def _record_decision(question: str, card: QuestionCard, chosen: CardOption, session: Session | None) -> None:
    marker = f"{_DECISION_MARKER}{card.decision.value}:{question_fingerprint(question)}"
    with transaction.atomic():
        row = DeferredQuestion.record(question, dedupe_marker=marker, task_session=session)
        row.apply_answer(chosen.label, resolved_via=DeferredQuestion.ResolvedVia.AGENT)
        DeferredQuestionAudit.objects.create(
            question=row,
            action="answered",
            answer_text=chosen.label,
            note="The only option that crosses none of the owner's four limits, from the fixed table.",
            resolver_id=_RESOLVER,
        )


def settle_before_recording(
    question: str, card: QuestionCard, dedupe_marker: str, session: Session | None
) -> str | None:
    """What the asking agent is told when the code settled *card* without raising it; ``None`` to record it."""
    if card.problems(question):
        return None
    if (basis := _recent_owner_answer(question, card, dedupe_marker)) is not None:
        return (
            f"The owner already answered this (question #{basis.pk}): {basis.answer_text} "
            "Nothing was recorded and nothing goes to the owner; go on with that answer."
        )
    if (chosen := _limit_choice(card)) is None:
        return None
    _record_decision(question, card, chosen, session)
    return f"Decided by the fixed limit table: {chosen.label}. {chosen.description} Nothing goes to the owner; go on."
