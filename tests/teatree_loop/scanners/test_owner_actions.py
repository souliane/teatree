"""The global tick reports each answered action card it carried out, declined or refused."""

from unittest.mock import Mock, patch

import pytest
from django.db import OperationalError
from django.test import TestCase

from teatree.core.modelkit.owner_action import ActionRequest, OwnerActionKind, action_marker
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard
from teatree.core.models import DeferredQuestion
from teatree.loop.question_binding import answer_from_click
from teatree.loop.scanners.owner_actions import OwnerActionScanner


class TestOwnerActionScanner(TestCase):
    def test_scanner_emits_a_real_answered_action_result_once(self) -> None:
        action = ActionRequest(kind=OwnerActionKind.VOID, target="99999", binding="digest")
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
        assert row.mark_mirrored(channel="D0DEMOOWNER", slack_ts="1800000000.000100")
        assert answer_from_click(
            {
                "user": {"id": "U0DEMOOWNER"},
                "channel": {"id": row.slack_channel},
                "message": {"ts": row.slack_ts},
                "actions": [{"action_id": "owner-question-option-1", "value": "1", "text": {"text": "Accept"}}],
            },
            backend=Mock(user_id="U0DEMOOWNER"),
        )

        with patch("teatree.core.answering.owner_actions.notify_user"):
            signals = OwnerActionScanner().scan()

        assert [(signal.kind, signal.summary) for signal in signals] == [("owner_action.result", f"refused {row.pk}")]
        assert OwnerActionScanner().scan() == []


def test_scanner_surfaces_an_unreadable_action_queue() -> None:
    with (
        patch(
            "teatree.core.answering.owner_actions.DeferredQuestion.objects.awaiting_action_outcome",
            side_effect=OperationalError("unreadable"),
        ),
        pytest.raises(OperationalError, match="unreadable"),
    ):
        OwnerActionScanner().scan()
