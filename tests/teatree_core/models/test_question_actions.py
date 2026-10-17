"""The owner action answer helper reads the card's recorded choices."""

import pytest
from django.test import TestCase

from teatree.core.modelkit.owner_action import ActionRequest, OwnerActionKind, action_marker
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard
from teatree.core.models import DeferredQuestion
from teatree.core.models.deferred_question import DeferredQuestionError


class TestQuestionActions(TestCase):
    def test_record_refuses_an_action_marker_without_the_matching_card(self) -> None:
        action = ActionRequest(kind=OwnerActionKind.ATTEST, target="17", binding="digest")
        marker = action_marker(action)
        with pytest.raises(DeferredQuestionError, match="reserved"):
            DeferredQuestion.record("Should I accept this checkout?", dedupe_marker=marker)
        card = QuestionCard(
            decision=OwnerDecision.IRREVERSIBLE,
            checked=("The checkout was inspected.",),
            blocker="Only the owner may accept this state.",
            why="The factory stopped before writing.",
            options=(
                CardOption("Proceed", "I record this checkout state.", recommended=True),
                CardOption("Leave", "The task stays parked."),
            ),
            action=action,
        )
        with pytest.raises(DeferredQuestionError, match="reserved"):
            DeferredQuestion.record(
                "Should I accept this checkout?", dedupe_marker="owner-action:attest:wrong", card=card
            )
        with pytest.raises(DeferredQuestionError, match="reserved"):
            DeferredQuestion.record("Should I accept this checkout?", dedupe_marker="other", card=card)
        assert not DeferredQuestion.objects.exists()
