"""A coordinator's message_peer reaches the one live agent working on the ticket, over the worker's mailbox (#5685)."""

import asyncio
import tempfile
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.agents import live_mailbox
from teatree.agents.coordinator_peer_messenger import send_to_ticket_peer
from teatree.agents.live_delivery import MailboxIdentity
from teatree.agents.live_mailbox_client import MailboxClient, MailboxError
from teatree.core.models import Ticket


@contextmanager
def _broker() -> Generator[live_mailbox.LiveMailboxBroker]:
    with (
        tempfile.TemporaryDirectory(prefix="t3-peer-", dir="/tmp") as directory,
        live_mailbox.LiveMailboxBroker(runtime_dir=Path(directory)) as broker,
        patch.object(live_mailbox, "shared_broker", return_value=broker),
    ):
        yield broker


def _read(peer: MailboxIdentity) -> list[str]:
    page = asyncio.run(MailboxClient(peer.socket_path, peer.token).wait(timeout_seconds=3))
    return [message["text"] for message in page["messages"]]


class TestTheMessageGoesToExactlyOneLiveAgentOnTheTicket(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(issue_url="https://github.com/example-org/example-repo/issues/4084")

    def _send_while_reading(self, reader: MailboxIdentity, text: str) -> tuple[bool, str, list[str]]:
        with ThreadPoolExecutor(max_workers=1) as pool:
            reading = pool.submit(_read, reader)
            receipt = send_to_ticket_peer(self.ticket, text, "k1")
            return receipt.accepted, receipt.detail, reading.result(timeout=5)

    def test_the_agent_on_the_ticket_receives_the_text_and_accepts_it(self) -> None:
        with _broker() as broker:
            agent = broker.register(room=str(self.ticket.pk), harness="claude_sdk", label="coding task 7")

            accepted, detail, received = self._send_while_reading(agent, "Use approach B.")

        assert (accepted, detail, received) == (True, "", ["Use approach B."])

    def test_a_message_the_agent_has_not_read_yet_is_not_claimed_as_sent(self) -> None:
        with _broker() as broker:
            broker.register(room=str(self.ticket.pk), harness="claude_sdk", label="coding task 7")

            receipt = send_to_ticket_peer(self.ticket, "Use approach B.", "k1")

        assert (receipt.accepted, receipt.detail) == (False, "the agent has not read it yet; it stays queued")

    def test_no_live_agent_on_the_ticket_sends_nothing(self) -> None:
        with _broker() as broker:
            other = broker.register(room=str(self.ticket.pk + 1), harness="claude_sdk", label="coding task 8")

            receipt = send_to_ticket_peer(self.ticket, "Use approach B.", "k1")
            inbox = asyncio.run(MailboxClient(other.socket_path, other.token).inbox())["messages"]

        assert (receipt.accepted, receipt.detail) == (False, "no live agent is working on that ticket")
        assert inbox == []

    def test_a_session_that_is_not_bound_to_a_ticket_is_never_chosen(self) -> None:
        with _broker() as broker:
            broker.register(room="", harness=live_mailbox.OPERATOR_HARNESS, label="owner session")

            receipt = send_to_ticket_peer(self.ticket, "Use approach B.", "k1")

        assert receipt.detail == "no live agent is working on that ticket"

    def test_an_agent_that_cannot_receive_is_not_a_candidate(self) -> None:
        with _broker() as broker:
            broker.register(room=str(self.ticket.pk), harness="claude_sdk", label="coding task 7", can_receive=False)

            receipt = send_to_ticket_peer(self.ticket, "Use approach B.", "k1")

        assert receipt.detail == "no live agent is working on that ticket"

    def test_two_live_agents_on_the_ticket_are_ambiguous_and_neither_is_messaged(self) -> None:
        with _broker() as broker:
            first = broker.register(room=str(self.ticket.pk), harness="claude_sdk", label="coding task 7")
            second = broker.register(room=str(self.ticket.pk), harness="claude_sdk", label="testing task 9")

            receipt = send_to_ticket_peer(self.ticket, "Use approach B.", "k1")
            inboxes = [
                asyncio.run(MailboxClient(peer.socket_path, peer.token).inbox())["messages"] for peer in (first, second)
            ]

        assert (receipt.accepted, receipt.detail) == (False, "more than one live agent is working on that ticket")
        assert inboxes == [[], []]

    def test_the_sender_identity_is_gone_once_the_message_is_handed_over(self) -> None:
        with _broker() as broker:
            agent = broker.register(room=str(self.ticket.pk), harness="claude_sdk", label="coding task 7")
            self._send_while_reading(agent, "Use approach B.")

            remaining = [peer["address"] for peer in broker.identities()]

        assert remaining == [agent.address]

    def test_a_mailbox_that_cannot_be_used_is_reported_not_raised(self) -> None:
        for failure in (RuntimeError("did not start"), OSError("socket"), ValueError("room"), MailboxError("gone")):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(live_mailbox, "shared_broker", side_effect=failure):
                    receipt = send_to_ticket_peer(self.ticket, "Use approach B.", "k1")

                assert (receipt.accepted, receipt.detail) == (False, "the live mailbox could not be reached")

    def test_a_refusal_that_is_not_a_queue_names_its_code_and_waits_two_seconds_for_acceptance(self) -> None:
        refused = {"id": 4, "to": "x", "outcome": "rejected", "code": "session_closed", "queued_in_inbox": False}
        with _broker() as broker:
            broker.register(room=str(self.ticket.pk), harness="claude_sdk", label="coding task 7")
            with patch.object(MailboxClient, "send", return_value=refused) as send:
                receipt = send_to_ticket_peer(self.ticket, "Use approach B.", "k1")

        assert (receipt.accepted, receipt.detail) == (False, "the agent did not take it (session_closed)")
        assert send.call_args.kwargs["timeout_seconds"] == 2

    def test_it_works_from_inside_a_running_event_loop(self) -> None:
        async def from_a_coroutine() -> str:
            await asyncio.sleep(0)
            return send_to_ticket_peer(self.ticket, "Use approach B.", "k1").detail

        with _broker():
            assert asyncio.run(from_a_coroutine()) == "no live agent is working on that ticket"
