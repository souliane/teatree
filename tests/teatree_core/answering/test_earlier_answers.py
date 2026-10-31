"""A repeat card on a subject the owner already answered is refused with their own words, never answered by code."""

import dataclasses
from datetime import timedelta

from django.test import TestCase

from teatree.core.answering.earlier_answers import DIFFERS_PREFIX, MAX_SHOWN_CHARS, earlier_answer_problem
from teatree.core.modelkit.owner_decision import HELD_PR_MARKER_PREFIX, OWNER_RAISABLE_DECISIONS, OwnerDecision
from teatree.core.modelkit.question_card import QuestionCard
from teatree.core.models.deferred_question import DeferredQuestion
from tests._owner_channel import answered_owner_row, owner_card
from tests.factories import planned_ticket

EARLIER = "May I use the deploy credential for this run?"
ANSWER = "Use the vault entry for this run only."
REPEAT = "Could I borrow the deploy token for the build?"


def _card(decision: OwnerDecision = OwnerDecision.CREDENTIALS, *, why: str = "The build needs it.") -> QuestionCard:
    return dataclasses.replace(owner_card(decision), why=why)


class TestARepeatCardIsRefusedWithTheOwnersWords(TestCase):
    def test_a_reworded_repeat_shows_the_earlier_question_and_answer_and_the_way_forward(self) -> None:
        ticket = planned_ticket()
        answered_owner_row(ticket, EARLIER, ANSWER)

        problem = earlier_answer_problem(ticket.pk, _card(), REPEAT)

        assert EARLIER in problem
        assert ANSWER in problem
        assert DIFFERS_PREFIX in problem

    def test_every_raisable_kind_is_refused_including_the_kept_ones(self) -> None:
        for kind in OWNER_RAISABLE_DECISIONS:
            with self.subTest(kind=kind):
                ticket = planned_ticket()
                answered_owner_row(ticket, EARLIER, ANSWER, decision=kind)

                assert ANSWER in earlier_answer_problem(ticket.pk, _card(kind), REPEAT)

    def test_a_why_that_states_the_difference_is_not_refused(self) -> None:
        ticket = planned_ticket()
        answered_owner_row(ticket, EARLIER, ANSWER)

        why = f"  {DIFFERS_PREFIX} the build now needs a second host."

        assert earlier_answer_problem(ticket.pk, _card(why=why), REPEAT) == ""

    def test_only_the_three_newest_answers_are_shown(self) -> None:
        ticket = planned_ticket()
        for hours in (5, 4, 3, 2):
            answered_owner_row(ticket, f"Question {hours}?", f"Answer {hours}.", ago=timedelta(hours=hours))

        problem = earlier_answer_problem(ticket.pk, _card(), REPEAT)

        assert [f"Answer {hours}." in problem for hours in (2, 3, 4, 5)] == [True, True, True, False]

    def test_a_long_answer_is_shown_cut_short(self) -> None:
        ticket = planned_ticket()
        answered_owner_row(ticket, EARLIER, "x" * (MAX_SHOWN_CHARS + 50))

        problem = earlier_answer_problem(ticket.pk, _card(), REPEAT)

        assert "x" * MAX_SHOWN_CHARS in problem
        assert "x" * (MAX_SHOWN_CHARS + 1) not in problem


class TestWhatIsNotAnEarlierAnswer(TestCase):
    def test_no_ticket_means_nothing_to_read(self) -> None:
        assert earlier_answer_problem(None, _card(), REPEAT) == ""

    def test_an_answer_that_is_not_the_owners_on_this_ticket_and_kind_in_the_last_day_does_not_count(self) -> None:
        cases: dict[str, dict[str, object]] = {
            "answered over a day ago": {"ago": timedelta(hours=25)},
            "answered by an agent": {"resolved_via": DeferredQuestion.ResolvedVia.AGENT},
            "an internal row": {"audience": DeferredQuestion.Audience.INTERNAL},
            "another kind": {"decision": OwnerDecision.ARCHITECTURE},
            "a held-pull-request wait": {"dedupe_marker": f"{HELD_PR_MARKER_PREFIX}7"},
            "an action card": {"evidence": owner_card(OwnerDecision.CREDENTIALS).to_evidence() | {"action": {}}},
        }
        for name, fields in cases.items():
            with self.subTest(name):
                ticket = planned_ticket()
                answered_owner_row(ticket, EARLIER, ANSWER, **fields)

                assert earlier_answer_problem(ticket.pk, _card(), REPEAT) == ""

    def test_an_answer_on_another_ticket_does_not_count(self) -> None:
        answered_owner_row(planned_ticket(), EARLIER, ANSWER)

        assert earlier_answer_problem(planned_ticket().pk, _card(), REPEAT) == ""

    def test_the_same_question_asked_again_is_left_to_the_dedupe_that_hands_the_answer_back(self) -> None:
        ticket = planned_ticket()
        answered_owner_row(ticket, EARLIER, ANSWER)

        assert earlier_answer_problem(ticket.pk, _card(), f"  {EARLIER.lower()}  ") == ""
