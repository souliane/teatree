"""An owner answer on a card with no parked task opens one named task for the step it approves (#5096)."""

from django.test import TestCase

from teatree.core.modelkit.owner_decision import OwnerDecision, agent_card_marker
from teatree.core.models import DeferredQuestion, Session, Task, Ticket
from teatree.core.models.question_text import question_fingerprint
from teatree.core.models.task_handoff import resume_on_answer
from tests._owner_channel import OWNER_CARD, answer_on_slack
from tests.factories import planned_ticket

QUESTION = "Should the factory publish the release notes by hand?"


class TestAnApprovedStepBecomesOwned(TestCase):
    def setUp(self) -> None:
        self.ticket = planned_ticket(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
        self.session = Session.objects.create(ticket=self.ticket, agent_id="coding")
        self.prior = Task.objects.create(
            ticket=self.ticket, session=self.session, phase="shipping", status=Task.Status.COMPLETED
        )

    def _agent_card(self, *, marker: str = "") -> DeferredQuestion:
        return DeferredQuestion.record(
            QUESTION,
            card=OWNER_CARD,
            task_session=self.session,
            dedupe_marker=marker or agent_card_marker(OwnerDecision.ARCHITECTURE, question_fingerprint(QUESTION)),
        )

    def _minted(self) -> list[Task]:
        return list(Task.objects.filter(ticket=self.ticket).exclude(pk=self.prior.pk))

    def test_answering_the_recommended_option_opens_exactly_one_named_task(self) -> None:
        card = self._agent_card()

        answer_on_slack(card, "Yes")
        card.refresh_from_db()
        repeat = DeferredQuestion.record(QUESTION, card=OWNER_CARD, dedupe_marker=card.dedupe_marker)

        (task,) = self._minted()
        assert task.phase == "shipping"
        assert task.status == Task.Status.PENDING
        assert task.execution_reason.startswith(f"Owner approved: {QUESTION} -> Yes: I go ahead.")
        assert repeat.pk == card.pk
        assert DeferredQuestion.objects.count() == 1

    def test_the_same_card_never_opens_a_second_task(self) -> None:
        card = self._agent_card()
        answer_on_slack(card, "Yes")
        card.refresh_from_db()

        again = resume_on_answer(card, None)

        assert again is not None
        assert len(self._minted()) == 1

    def test_answering_a_declining_option_opens_no_task(self) -> None:
        answer_on_slack(self._agent_card(), "No")

        assert self._minted() == []

    def test_a_card_whose_producer_reads_its_own_answer_opens_no_task(self) -> None:
        answer_on_slack(self._agent_card(marker="backlog-sweep-folds-7"), "Yes")

        assert self._minted() == []

    def test_a_card_with_no_subject_ticket_opens_no_task(self) -> None:
        card = DeferredQuestion.record(
            QUESTION,
            card=OWNER_CARD,
            dedupe_marker=agent_card_marker(OwnerDecision.ARCHITECTURE, question_fingerprint(QUESTION)),
        )

        answer_on_slack(card, "Yes")

        assert Task.objects.count() == 1
