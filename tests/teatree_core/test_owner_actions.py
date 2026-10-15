"""A Slack answer cannot execute a card whose action identity was changed."""

from django.test import TestCase

from teatree.core.modelkit.owner_action import ActionRequest, OwnerActionKind
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard
from teatree.core.models import DeferredQuestion, DeferredQuestionAudit
from teatree.core.owner_actions import carry_out_owner_actions
from teatree.loop.question_binding import BoundAnswer, apply_bound_answer


class TestOwnerActions(TestCase):
    def test_a_mismatched_marker_refuses_the_action_once(self) -> None:
        action = ActionRequest(kind=OwnerActionKind.ATTEST, target="1", binding="digest")
        card = QuestionCard(
            decision=OwnerDecision.IRREVERSIBLE,
            checked=("The checkout changed.",),
            blocker="Only the owner may accept it.",
            why="The factory stopped before writing.",
            options=(
                CardOption("Accept", "I record this state.", recommended=True),
                CardOption("Decline", "Nothing changes."),
            ),
            action=action,
        )
        row = DeferredQuestion.record("Should I accept the checkout?", dedupe_marker="other", card=card)
        assert apply_bound_answer(BoundAnswer(row, "Accept", answered_by="U0DEMOOWNER"))
        assert carry_out_owner_actions() == [f"refused {row.pk}"]
        assert carry_out_owner_actions() == []
        assert DeferredQuestionAudit.objects.filter(question=row, action="refused").count() == 1
