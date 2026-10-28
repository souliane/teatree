from django.test import TestCase

from teatree.core.models import DeferredQuestion, Session, Task, Ticket
from teatree.core.owner_dm.question_ticket import single_question_ticket_id
from tests._owner_channel import OWNER_DECISION


class TestSingleQuestionTicket(TestCase):
    def test_parked_task_precedes_session_and_missing_subject_stays_unbound(self) -> None:
        task_ticket = Ticket.objects.create(short_description="Task")
        session_ticket = Ticket.objects.create(short_description="Session")
        task = Task.objects.create(
            ticket=task_ticket, session=Session.objects.create(ticket=task_ticket), phase="coding"
        )
        question = DeferredQuestion.record("Continue?", session_id="s1", **OWNER_DECISION)
        question.parked_task = task
        question.task_session = Session.objects.create(ticket=session_ticket)
        question.save(update_fields=["parked_task", "task_session"])

        assert single_question_ticket_id(question) == task_ticket.pk
        question.dedupe_marker = "repair-halt:unknown"
        assert single_question_ticket_id(question) is None

    def test_stuck_marker_names_its_live_ticket(self) -> None:
        ticket = Ticket.objects.create(short_description="Stuck")
        question = DeferredQuestion.record("Continue?", session_id="s2", **OWNER_DECISION)
        question.dedupe_marker = f"stuck-redispatch-halt:{ticket.pk}"

        assert single_question_ticket_id(question) == ticket.pk
