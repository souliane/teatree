"""Shared owner-channel test helpers: ask the owner a genuine decision, answer it through the real Slack seam."""

from collections.abc import Sequence
from typing import TypedDict

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.question_binding import BoundAnswer, apply_bound_answer

OWNER_SLACK_ID = "U1"


class OwnerDecisionFields(TypedDict):
    decision: OwnerDecision
    checked: Sequence[str]


OWNER_DECISION = OwnerDecisionFields(decision=OwnerDecision.PRODUCT_SCOPE, checked=("no standing answer covers it",))


def answer_on_slack(question: DeferredQuestion, text: str) -> None:
    assert apply_bound_answer(BoundAnswer(question=question, answer=text))
