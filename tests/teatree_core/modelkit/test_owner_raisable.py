"""Owner-card kinds accepted by the durable question entry point."""

import pytest
from django.test import TestCase

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import QuestionCard
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionError


class TestOwnerRaisableDecisions(TestCase):
    def _card(self, decision: OwnerDecision) -> QuestionCard:
        return QuestionCard(
            decision=decision,
            checked=("The facts were checked.",),
            blocker="The decision needs the owner.",
            why="The next action depends on this choice.",
        )

    def test_legacy_scope_cannot_create_a_new_owner_card(self) -> None:
        with pytest.raises(DeferredQuestionError, match="product_scope"):
            DeferredQuestion.record("Choose the scope?", card=self._card(OwnerDecision.PRODUCT_SCOPE))
        assert DeferredQuestion.objects.count() == 0

    def test_four_limits_and_design_still_record(self) -> None:
        for decision in (
            OwnerDecision.CREDENTIALS,
            OwnerDecision.MONEY_OR_PLAN,
            OwnerDecision.PUBLIC_POST,
            OwnerDecision.IRREVERSIBLE,
            OwnerDecision.ARCHITECTURE,
        ):
            with self.subTest(decision=decision):
                row = DeferredQuestion.record("Choose the action?", card=self._card(decision))
                assert row.audience == DeferredQuestion.Audience.OWNER_QUESTION

    def test_legacy_scope_row_remains_readable(self) -> None:
        row = DeferredQuestion.objects.create(
            question="Old scope question?",
            audience=DeferredQuestion.Audience.OWNER_QUESTION,
            evidence=self._card(OwnerDecision.PRODUCT_SCOPE).to_evidence(),
        )
        assert QuestionCard.from_row(row.evidence, row.options_json).decision is OwnerDecision.PRODUCT_SCOPE
