"""An owner's first message in an unbound thread leaves one conversation ticket bound to it (#5685)."""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import DmContext, PendingChatInjection, Task, ThreadBinding, Ticket
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle
from tests.teatree_loop.slack_answer.test_cycle import _USER_UID, RecordingBackend, _resolver

_STATUSLINE = "teatree.loop.slack_answer.simple_answer.statusline_for_slack"


def _say(text: str, *, ts: str, thread_ts: str = "") -> None:
    row = PendingChatInjection.record(
        channel="C1", slack_ts=ts, text=text, context=DmContext(user_id=_USER_UID, thread_ts=thread_ts)
    )
    assert row is not None


def _cycle(backend: RecordingBackend) -> None:
    with patch(_STATUSLINE, return_value="overlay=acme\nticket=#1\n"):
        run_slack_answer_cycle(messaging_resolver=_resolver(backend))


class TestFirstContactBinding(TestCase):
    def setUp(self) -> None:
        self.backend = RecordingBackend()

    def _conversation(self) -> Ticket:
        binding = ThreadBinding.objects.get()
        assert binding.thread_ts == "1.0"
        return binding.ticket

    def test_a_noted_message_binds_its_thread_to_one_conversation_ticket(self) -> None:
        _say("thanks!", ts="1.0")

        _cycle(self.backend)

        ticket = self._conversation()
        assert (ticket.is_conversation(), ticket.owes_no_work(), Ticket.objects.count()) == (True, True, 1)

    def test_a_simple_answer_binds_its_thread_to_one_conversation_ticket(self) -> None:
        _say("what's the status?", ts="1.0")

        _cycle(self.backend)

        assert len(self.backend.replies) == 1
        ticket = self._conversation()
        assert (ticket.is_conversation(), ticket.owes_no_work(), Ticket.objects.count()) == (True, True, 1)

    def test_a_work_implying_message_binds_the_same_way(self) -> None:
        _say("fix the failing pipeline", ts="1.0")

        _cycle(self.backend)

        ticket = self._conversation()
        assert (ticket.is_conversation(), ticket.owes_no_work(), Ticket.objects.count()) == (True, False, 1)

    def test_a_second_simple_message_routes_by_the_binding_and_creates_nothing(self) -> None:
        _say("thanks!", ts="1.0")
        _cycle(self.backend)
        _say("what's the status?", ts="2.0", thread_ts="1.0")

        _cycle(self.backend)

        assert (Ticket.objects.count(), ThreadBinding.objects.count(), Task.objects.count()) == (1, 1, 0)
        assert len(self.backend.replies) == 1

    def test_a_work_implying_reply_takes_the_bound_conversation_instead_of_a_second_ticket(self) -> None:
        _say("thanks!", ts="1.0")
        _cycle(self.backend)
        _say("fix the failing pipeline", ts="2.0", thread_ts="1.0")

        _cycle(self.backend)

        task = Task.objects.get(phase="answering")
        assert (Ticket.objects.count(), task.ticket_id) == (1, self._conversation().pk)
        assert task.ticket.owes_no_work() is False

    def test_a_rerun_of_the_same_cycle_binds_once(self) -> None:
        _say("thanks!", ts="1.0")

        _cycle(self.backend)
        _cycle(self.backend)

        assert (Ticket.objects.count(), ThreadBinding.objects.count()) == (1, 1)
