# test-path: cross-cutting — drives the questions command through the read-before-raise check in teatree.core.answering.
"""``t3 teatree questions record`` shows the owner's earlier answer on the ticket before it raises a repeat card."""

import json
from unittest.mock import patch

from django.test import TestCase

from teatree.core.answering.earlier_answers import DIFFERS_PREFIX
from teatree.core.models.deferred_question import DeferredQuestion
from tests._owner_channel import answered_owner_row
from tests.factories import planned_ticket
from tests.teatree_core.management.commands.test_questions_record_card import _BLOCKER, _OPTIONS, _record

EARLIER = "May I use the deploy credential for this run?"
ANSWER = "Use the vault entry for this run only."
REPEAT = "Could I borrow the deploy token for the build?"


def _flags(why: str) -> tuple[str, ...]:
    return (
        "--decision",
        "credentials",
        "--checked",
        "The runbook names no other route.",
        "--why",
        why,
        "--blocker",
        _BLOCKER,
        "--options",
        json.dumps(_OPTIONS),
    )


class TestRecordReadsTheOwnersEarlierAnswer(TestCase):
    def setUp(self) -> None:
        self.ticket = planned_ticket()
        answered_owner_row(self.ticket, EARLIER, ANSWER)
        invoking = patch("teatree.core.management.commands.questions.resolve_invoking_ticket")
        invoking.start().return_value = self.ticket
        self.addCleanup(invoking.stop)

    def test_a_reworded_repeat_is_refused_with_the_answer_and_records_no_card(self) -> None:
        code, out, err = _record(REPEAT, *_flags("The build needs a token."))

        assert code == 2
        assert out == ""
        assert ANSWER in err
        assert DIFFERS_PREFIX in err
        assert DeferredQuestion.objects.count() == 1

    def test_a_why_that_states_the_difference_records_the_card(self) -> None:
        code, _out, _err = _record(REPEAT, *_flags(f"{DIFFERS_PREFIX} the build now needs a second host."))

        assert code == 0
        assert DeferredQuestion.objects.filter(question=REPEAT, answered_at__isnull=True).count() == 1
