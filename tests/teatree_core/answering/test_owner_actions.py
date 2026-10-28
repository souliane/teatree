"""Owner actions bind a reply to the recorded request."""

from unittest.mock import MagicMock

from django.test import TestCase

from teatree.core.answering.owner_actions import carry_out_owner_actions
from teatree.core.modelkit.owner_action import ActionRequest, OwnerActionKind, action_marker
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard
from teatree.core.models import DeferredQuestion, DeferredQuestionAudit
from teatree.loop.question_binding import answer_from_click

_OWNER = "U0DEMOOWNER"


def _tap(row: DeferredQuestion, label: str) -> dict[str, object]:
    return {
        "type": "block_actions",
        "user": {"id": _OWNER},
        "channel": {"id": row.slack_channel},
        "message": {"ts": row.slack_ts},
        "actions": [{"action_id": "owner-question-option-1", "value": "1", "text": {"text": label}}],
    }


def _owner_backend() -> MagicMock:
    backend = MagicMock(user_id=_OWNER)
    backend.update_message.return_value = {"ok": True}
    return backend


class TestActionIdentity(TestCase):
    def test_a_mismatched_marker_refuses_the_action_once(self) -> None:
        action = ActionRequest(kind=OwnerActionKind.VOID, target="1", binding="digest")
        card = QuestionCard(
            decision=OwnerDecision.IRREVERSIBLE,
            checked=("The request changed.",),
            blocker="Only the owner may approve it.",
            why="The factory stopped before writing.",
            options=(
                CardOption("Void", "I approve this request.", recommended=True),
                CardOption("Decline", "Nothing changes."),
            ),
            action=action,
        )
        row = DeferredQuestion.record("Should I approve the request?", dedupe_marker=action_marker(action), card=card)
        assert row.mark_mirrored(channel="D0DEMOOWNER", slack_ts="1800000000.000300")
        assert answer_from_click(_tap(row, "Void"), backend=_owner_backend())
        DeferredQuestion.objects.filter(pk=row.pk).update(dedupe_marker="other")

        assert carry_out_owner_actions() == [f"refused {row.pk}"]
        assert carry_out_owner_actions() == []
        assert DeferredQuestionAudit.objects.get(question=row, action="refused").note == (
            "The action card no longer matches its recorded request."
        )
