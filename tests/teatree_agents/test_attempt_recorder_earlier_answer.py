"""A headless owner stop on a subject the owner already answered is refused with the answer, never raised again."""

from django.test import TestCase

from teatree.agents.attempt_recorder import record_result_envelope
from teatree.core.answering.earlier_answers import DIFFERS_PREFIX
from teatree.core.modelkit.task_failure_taxonomy import OWNER_STOP_REFUSAL_PREFIX
from teatree.core.models import DeferredQuestion, Session, Task, Ticket
from tests._owner_channel import answered_owner_row, owner_stop
from tests.factories import planned_ticket

EARLIER = "May I use the deploy credential for this run?"
ANSWER = "Use the vault entry for this run only."
WORDINGS = (
    "Could I borrow the deploy token for the build?",
    "Is it fine to read the deploy secret during this run?",
    "Do I have your approval to reach the deploy credential?",
)


def _stop(reason: str, *, why: str = "") -> dict[str, object]:
    stop = owner_stop(reason) | {"summary": "waiting"}
    if why:
        card = stop["user_input_card"]
        assert isinstance(card, dict)
        stop["user_input_card"] = card | {"why": why}
    return stop


def _claimed(ticket: Ticket) -> Task:
    task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="scoping")
    task.claim(claimed_by="loop-slot")
    return task


def _owner_cards(task: Task) -> int:
    return DeferredQuestion.objects.filter(parked_task=task, audience=DeferredQuestion.Audience.OWNER_QUESTION).count()


class TestAHeadlessStopReadsTheOwnersEarlierAnswer(TestCase):
    def setUp(self) -> None:
        self.ticket = planned_ticket()
        answered_owner_row(self.ticket, EARLIER, ANSWER)

    def test_a_reworded_repeat_fails_the_attempt_with_the_answer_and_raises_no_card(self) -> None:
        task = _claimed(self.ticket)

        attempt = record_result_envelope(task, _stop(WORDINGS[0]))

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert attempt.error.startswith(OWNER_STOP_REFUSAL_PREFIX)
        assert ANSWER in attempt.error
        assert DIFFERS_PREFIX in attempt.error
        assert _owner_cards(task) == 0

    def test_a_why_that_states_the_difference_completes_and_raises_the_card(self) -> None:
        task = _claimed(self.ticket)

        attempt = record_result_envelope(
            task, _stop(WORDINGS[0], why=f"{DIFFERS_PREFIX} the build now needs a second host.")
        )

        assert attempt.error == ""
        assert _owner_cards(task) == 1

    def test_every_later_rewording_on_the_ticket_is_refused_too(self) -> None:
        for wording in WORDINGS[1:]:
            with self.subTest(wording=wording):
                task = _claimed(self.ticket)

                attempt = record_result_envelope(task, _stop(wording))

                assert attempt.error.startswith(OWNER_STOP_REFUSAL_PREFIX)
                assert _owner_cards(task) == 0

    def test_the_same_question_asked_again_still_resumes_through_its_dedupe(self) -> None:
        task = _claimed(self.ticket)
        record_result_envelope(task, _stop(WORDINGS[0], why=f"{DIFFERS_PREFIX} the build needs a second host."))
        row = DeferredQuestion.objects.get(parked_task=task)
        row.apply_answer("Use the vault entry.", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        again = _claimed(self.ticket)

        attempt = record_result_envelope(again, _stop(WORDINGS[0]))

        assert attempt.error == ""
        assert again.child_tasks.count() == 1
