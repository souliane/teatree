"""An owner card the owner answered or dismissed is returned by a repeat record, never asked again (#5096)."""

from unittest.mock import MagicMock

from django.test import TestCase

from teatree.core.models import DeferredQuestion
from teatree.core.notify_question_drains import (
    drain_deferred_questions,
    drain_unmirrored_deferred_questions,
    reask_escalated_questions,
)
from tests._owner_channel import OWNER_CARD, answer_on_slack

MARKER = "needs-input:7:an-answered-decision"
QUESTION = "Should the factory rotate the deploy token?"


def _backend() -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = "D0DEMOOWNER"
    backend.post_message.return_value = {"ok": True, "ts": "1700000000.000100"}
    backend.get_permalink.return_value = "https://acme.slack.example/archives/D0DEMOOWNER/p1700000000000100"
    return backend


def _drain_everything(backend: MagicMock) -> None:
    drain_unmirrored_deferred_questions(user_id="U0DEMOOWNER", backend=backend)
    drain_deferred_questions(user_id="U0DEMOOWNER", backend=backend)
    reask_escalated_questions(user_id="U0DEMOOWNER", backend=backend)


class TestADecidedCardIsNeverAskedAgain(TestCase):
    def test_a_repeat_record_of_an_answered_card_returns_the_same_row_and_nothing_posts(self) -> None:
        first = DeferredQuestion.record(QUESTION, card=OWNER_CARD, dedupe_marker=MARKER)
        answer_on_slack(first, "Yes")
        backend = _backend()

        again = DeferredQuestion.record(QUESTION, card=OWNER_CARD, dedupe_marker=MARKER)
        _drain_everything(backend)

        assert again.pk == first.pk
        assert DeferredQuestion.objects.count() == 1
        backend.post_message.assert_not_called()

    def test_a_repeat_record_of_a_dismissed_card_returns_the_dismissed_row_and_nothing_posts(self) -> None:
        first = DeferredQuestion.record(QUESTION, card=OWNER_CARD, dedupe_marker=MARKER)
        first.mark_stale("the ticket moved on", resolver_id="test")
        backend = _backend()

        again = DeferredQuestion.record(QUESTION, card=OWNER_CARD, dedupe_marker=MARKER)
        _drain_everything(backend)

        assert again.pk == first.pk
        assert not again.is_pending
        backend.post_message.assert_not_called()

    def test_a_card_nobody_decided_is_still_posted_once(self) -> None:
        DeferredQuestion.record(QUESTION, card=OWNER_CARD, dedupe_marker=MARKER)
        backend = _backend()

        drain_unmirrored_deferred_questions(user_id="U0DEMOOWNER", backend=backend)

        backend.post_message.assert_called_once()
