"""The question drains deliver a ticket's question to its requester, exactly as ``notify_user`` does."""

from unittest.mock import MagicMock

from django.contrib.auth import get_user_model
from django.test import TestCase

from teatree.core.models import DeferredQuestion, OperatorIdentity, Session, Task, Ticket
from teatree.core.notify_question_drains import drain_unmirrored_deferred_questions
from tests._owner_channel import OWNER_CARD


class DrainRoutesATicketQuestionToItsRequesterTestCase(TestCase):
    def setUp(self) -> None:
        operator = get_user_model().objects.create_superuser(username="alice", email="", password="unused")
        OperatorIdentity.objects.create(user=operator, platform="github", handle="alice", external_id="123")
        OperatorIdentity.objects.create(user=operator, platform="slack", handle="U-OP")

    @staticmethod
    def _question(requested_by: str) -> DeferredQuestion:
        ticket = Ticket.objects.create(
            overlay="teatree", issue_url="https://github.com/acme/app/issues/70", requested_by=requested_by
        )
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        return DeferredQuestion.record("Which route?", parked_task=task, card=OWNER_CARD)

    @staticmethod
    def _backend() -> MagicMock:
        backend = MagicMock()
        backend.open_dm.return_value = "D-DM"
        backend.post_message.return_value = {"ok": True, "ts": "1700000000.000001"}
        backend.fetch_thread_replies.return_value = [{"ts": "1700000000.000001", "text": "ticket header"}]
        backend.get_permalink.return_value = "https://slack.example/archives/D-DM/p1700000000000001"
        return backend

    def test_a_registered_requesters_question_opens_that_requesters_dm(self) -> None:
        self._question("github:alice")
        backend = self._backend()

        delivered, _total = drain_unmirrored_deferred_questions(user_id="U_ME", backend=backend)

        assert delivered == 1
        backend.open_dm.assert_called_once_with("U-OP")

    def test_a_question_with_no_recorded_requester_opens_the_owners_dm(self) -> None:
        self._question("")
        backend = self._backend()

        delivered, _total = drain_unmirrored_deferred_questions(user_id="U_ME", backend=backend)

        assert delivered == 1
        backend.open_dm.assert_called_once_with("U_ME")
