"""The owner action answer helper reads the card's recorded choices."""

from django.test import TestCase

from teatree.core.modelkit.owner_action import ActionRequest, OwnerActionKind, action_marker
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard
from teatree.core.models import DeferredQuestion
from teatree.core.models.question_actions import canonical_action_answer


class TestQuestionActions(TestCase):
    def test_action_answer_uses_options_and_preserves_unknown_replies(self) -> None:
        action = ActionRequest(kind=OwnerActionKind.ATTEST, target="17", binding="digest")
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
        row = DeferredQuestion.record("Should I accept this checkout?", dedupe_marker=action_marker(action), card=card)

        assert canonical_action_answer(row, "yes") == "Proceed"
        assert canonical_action_answer(row, "Leave") == "Leave"
        assert canonical_action_answer(row, "maybe") is None

    def test_plain_question_keeps_its_free_text(self) -> None:
        row = DeferredQuestion.record("What should I do next?")

        assert canonical_action_answer(row, "anything") == "anything"
