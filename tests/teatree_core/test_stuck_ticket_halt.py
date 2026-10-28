from django.test import TestCase

from teatree.core.models import Session, Task, Ticket
from teatree.core.stuck_ticket_halt import record_ticket_halt, ticket_halt_is_current


class TestStuckTicketHalt(TestCase):
    def test_new_task_or_review_head_expires_the_halt(self) -> None:
        ticket = Ticket.objects.create(extra={"reviewed_sha": "first"})
        assert not ticket_halt_is_current(ticket)

        record_ticket_halt(ticket, reason="spent")
        assert ticket_halt_is_current(ticket)
        assert ticket.extra["stuck_halt"]["reason"] == "spent"

        session = Session.objects.create(ticket=ticket, agent_id="planning")
        Task.objects.create(ticket=ticket, session=session, phase="planning")
        assert not ticket_halt_is_current(ticket)

        record_ticket_halt(ticket, reason="still spent")
        assert ticket_halt_is_current(ticket)
        ticket.merge_extra(set_keys={"reviewed_sha": "second"})
        assert not ticket_halt_is_current(ticket)
