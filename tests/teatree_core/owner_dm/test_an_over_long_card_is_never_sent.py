"""A card past the word cap is refused at record time, and a stored one is withheld, never posted (#4990, #5096)."""

import dataclasses
from unittest.mock import MagicMock

import pytest
from django.test import TestCase

from teatree.core.modelkit.question_card import CardOption
from teatree.core.models import DeferredQuestion, DeferredQuestionAudit
from teatree.core.models.deferred_question import DeferredQuestionError
from teatree.core.notify_question_drains import drain_unmirrored_deferred_questions
from tests._owner_channel import OWNER_CARD

_SENTENCE = " ".join(["go"] * 50)
OVER_LONG = dataclasses.replace(
    OWNER_CARD,
    why=f"{_SENTENCE}.",
    options=(
        CardOption("Yes", f"{_SENTENCE}.", recommended=True),
        CardOption("No", f"{_SENTENCE}."),
    ),
)
QUESTION = f"{' '.join(['go'] * 40)}?"


class TestAnOverLongCardIsNeverSent(TestCase):
    def test_recording_it_is_refused_and_creates_no_row(self) -> None:
        with pytest.raises(DeferredQuestionError, match="words"):
            DeferredQuestion.record(QUESTION, card=OVER_LONG)

        assert not DeferredQuestion.objects.exists()

    def test_a_stored_over_long_row_is_withheld_and_never_posted(self) -> None:
        row = DeferredQuestion.objects.create(
            question=QUESTION,
            audience=DeferredQuestion.Audience.OWNER_QUESTION,
            evidence=OVER_LONG.to_evidence(),
            options_json=OVER_LONG.options_json(),
        )
        backend = MagicMock()

        drain_unmirrored_deferred_questions(user_id="U0DEMOOWNER", backend=backend)

        backend.post_message.assert_not_called()
        row.refresh_from_db()
        assert row.audience == DeferredQuestion.Audience.INTERNAL
        assert DeferredQuestionAudit.objects.filter(question=row, action="withheld").exists()
