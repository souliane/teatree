"""The global tick reports each answered action card it carried out, declined or refused."""

from unittest.mock import patch

import pytest
from django.db import OperationalError
from django.test import TestCase

from teatree.core.modelkit.owner_action import ActionRequest, OwnerActionKind, action_marker
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard
from teatree.core.models import DeferredQuestion
from teatree.loop.question_binding import BoundAnswer, apply_bound_answer
from teatree.loop.scanners.owner_actions import OwnerActionScanner


class TestOwnerActionScanner(TestCase):
    def test_scanner_emits_a_real_answered_action_result_once(self) -> None:
        action = ActionRequest(kind=OwnerActionKind.ATTEST, target="99999", binding="digest")
        card = QuestionCard(
            decision=OwnerDecision.IRREVERSIBLE,
            checked=("The checkout changed.",),
            blocker="Only the owner may accept this state.",
            why="The factory stopped before writing.",
            options=(
                CardOption("Accept", "I record this checkout state.", recommended=True),
                CardOption("Decline", "The task stays parked."),
            ),
            action=action,
        )
        row = DeferredQuestion.record("Should I accept this checkout?", dedupe_marker=action_marker(action), card=card)
        assert apply_bound_answer(BoundAnswer(row, "Accept", answered_by="U0DEMOOWNER"))

        with patch("teatree.core.answering.owner_actions.notify_user"):
            signals = OwnerActionScanner().scan()

        assert [(signal.kind, signal.summary) for signal in signals] == [("owner_action.result", f"refused {row.pk}")]
        assert OwnerActionScanner().scan() == []


def test_scanner_surfaces_an_unreadable_action_queue() -> None:
    with (
        patch(
            "teatree.core.answering.owner_actions.DeferredQuestion.objects.filter",
            side_effect=OperationalError("unreadable"),
        ),
        pytest.raises(OperationalError, match="unreadable"),
    ):
        OwnerActionScanner().scan()
