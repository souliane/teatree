from unittest.mock import MagicMock

from django.test import TestCase

from teatree.core.answering.owner_actions import carry_out_owner_actions
from teatree.core.gates.gate_escape_request import request_owner_escape
from teatree.core.modelkit.owner_action import OwnerActionKind, action_marker, read_action
from teatree.core.modelkit.question_card import QuestionCard, plain_text
from teatree.core.models.gate_escape import EscapeScope, GateEscape
from teatree.loop.question_binding import answer_from_click


def _scope(**changes: str) -> EscapeScope:
    fields = {
        "gate": "raw_review_post",
        "reason": "The normal route rejected this fixture action.",
        "actor": "coding-agent",
        "subject": "/work/demo-service",
        "head": "a" * 40,
        "action_digest": "b" * 64,
        "session_id": "session-1",
        "call_id": "call-1",
        "excerpt": "gh api repos/acme/demo-service/pulls/7/comments -f body=hello",
    }
    fields.update(changes)
    return EscapeScope(**fields)


class TestGateEscapeRequest(TestCase):
    def test_owner_question_carries_exact_scope(self) -> None:
        row = request_owner_escape(_scope())
        action = read_action(row.evidence)
        assert action is not None
        assert action["kind"] == OwnerActionKind.ESCAPE
        assert action["target"] == "raw_review_post"
        assert action["binding"] == _scope().binding()
        assert action["scope"]["head"] == "a" * 40
        assert row.dedupe_marker == action_marker(action)
        assert GateEscape.objects.count() == 0

    def test_owner_card_describes_the_blocked_action(self) -> None:
        row = request_owner_escape(_scope())
        card = QuestionCard.from_row(row.evidence, row.options_json)
        assert card is not None
        shown = plain_text(card.view(row.question))
        assert "review post" in shown
        assert "demo-service" in shown
        assert "gh api" in shown
        action = read_action(row.evidence)
        assert action is not None
        assert action["scope"]["excerpt"].startswith("gh api repos/acme/demo-service")

    def test_a_command_that_cannot_be_said_in_plain_words_still_reaches_the_owner(self) -> None:
        row = request_owner_escape(_scope(subject="/work/demo_service", excerpt='{"body": "x"}'))
        assert row.is_pending

    def test_owner_button_records_a_single_scoped_grant(self) -> None:
        row = request_owner_escape(_scope())
        assert row.mark_mirrored(channel="D0DEMOOWNER", slack_ts="1800000000.000300")
        backend = MagicMock(user_id="U0DEMOOWNER")
        backend.update_message.return_value = {"ok": True}
        click = {
            "type": "block_actions",
            "user": {"id": "U0DEMOOWNER"},
            "channel": {"id": row.slack_channel},
            "message": {"ts": row.slack_ts},
            "actions": [{"action_id": "owner-question-option-1", "value": "1", "text": {"text": "Approve"}}],
        }
        assert answer_from_click(click, backend=backend)
        assert carry_out_owner_actions() == [f"carried out {row.pk}"]
        grant = GateEscape.objects.get()
        assert grant.issuer_kind == GateEscape.Issuer.HUMAN
        assert grant.issuer_id == "slack:U0DEMOOWNER"
        assert grant.recorded_at is None
