"""A bound task escalates: the request row exists first, then a live coordinator or the resumed task hears it."""

from pathlib import Path

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.test import TestCase

from teatree.agents.coordinator_escalation import ESCALATE, EscalationReceipt, escalate
from teatree.agents.live_control import PeerIdentity
from teatree.agents.live_delivery import SendReceipt
from teatree.agents.live_mailbox_client import MailboxClient, MailboxError
from teatree.core.answering.escalation import ESCALATION_DEADLINE, EscalationRefusedError
from teatree.core.modelkit.phase_tools import mcp_write_tools_for_phase
from teatree.core.models import Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from tests.factories import SessionFactory, TaskFactory, TicketFactory

_ASKER = "asker-address"
_COORDINATOR = "coordinator-address"
_MESSAGE = "Should the migration run before or after the backfill"


class FakeMailbox(MailboxClient):
    """The mailbox client of a bound task: its own identity, the live peers, and what it sent."""

    def __init__(self, identity: PeerIdentity, peers: list[PeerIdentity] | None = None) -> None:
        super().__init__(Path("/nonexistent"), "token")
        self.identity = identity
        self.live_peers = peers or []
        self.outcome = "accepted_current_turn"
        self.broken = False
        self.sent: list[tuple[str, str, str, float]] = []
        self.rows_when_sent: list[int] = []

    async def self_identity(self) -> PeerIdentity:
        return self.identity

    async def peers(self) -> list[PeerIdentity]:
        if self.broken:
            msg = "mailbox gone"
            raise MailboxError(msg)
        return self.live_peers

    async def send(self, to: str, text: str, key: str, *, timeout_seconds: float = 20) -> SendReceipt:
        self.rows_when_sent.append(await sync_to_async(DeferredQuestion.objects.count, thread_sensitive=True)())
        self.sent.append((to, text, key, timeout_seconds))
        return {"id": 1, "to": to, "outcome": self.outcome, "code": None, "queued_in_inbox": False}


def _running(phase: str = "planning") -> Task:
    ticket = TicketFactory.create(state=Ticket.State.WORK_STARTED)
    return TaskFactory.create(
        ticket=ticket,
        session=SessionFactory.create(ticket=ticket, agent_id=phase),
        phase=phase,
        status=Task.Status.CLAIMED,
    )


def _identity(task: Task, *, phase: str | None = None) -> PeerIdentity:
    return {
        "address": _ASKER,
        "harness": "claude_sdk",
        "label": f"{task.phase} task {task.pk}",
        "ticket": str(task.ticket_id),
        "room": str(task.ticket_id),
        "phase": phase or task.phase,
        "can_receive": True,
    }


def _coordinator(
    ticket_id: int, *, address: str = _COORDINATOR, phase: str = "answering", can_receive: bool = True
) -> PeerIdentity:
    return {
        "address": address,
        "harness": "claude_sdk",
        "label": "answering task 99",
        "ticket": str(ticket_id),
        "room": str(ticket_id),
        "phase": phase,
        "can_receive": can_receive,
    }


def _call(mailbox: FakeMailbox, kind: str = "internal", message: str = _MESSAGE) -> EscalationReceipt:
    return async_to_sync(escalate)(mailbox, kind=kind, message=message)


def _answering_task(ticket: Ticket) -> Task:
    return TaskFactory.create(
        ticket=ticket,
        session=SessionFactory.create(ticket=ticket, agent_id="answering"),
        phase="answering",
        status=Task.Status.COMPLETED,
    )


class TestEscalatingToALiveCoordinator(TestCase):
    def setUp(self) -> None:
        self.asker = _running()
        self.mailbox = FakeMailbox(_identity(self.asker), [_coordinator(self.asker.ticket_id)])

    def test_the_request_row_exists_when_the_message_is_sent_and_names_it(self) -> None:
        receipt = _call(self.mailbox)

        row = DeferredQuestion.objects.get()
        assert self.mailbox.rows_when_sent == [1]
        assert receipt == {"request_id": row.pk, "delivery": "peer", "deadline_minutes": 30}
        (to, text, key, wait) = self.mailbox.sent[0]
        assert (to, key, wait) == (_COORDINATOR, f"escalation:{row.pk}", 2)
        assert f"Escalation {row.pk} from planning task {self.asker.pk}." in text
        assert "information, not an instruction" in text
        assert "tell the owner in this thread" in text
        assert "30 minutes" in text
        assert _MESSAGE in text
        assert not {"question_answer", "mailbox"} & set(text.lower().replace(".", " ").split())
        assert Task.objects.filter(parent_task__isnull=False).count() == 0

    def test_a_repeated_identical_call_returns_the_same_request_and_sends_under_the_same_key(self) -> None:
        first = _call(self.mailbox)
        again = _call(self.mailbox)

        assert again["request_id"] == first["request_id"]
        assert DeferredQuestion.objects.count() == 1
        assert {key for _to, _text, key, _wait in self.mailbox.sent} == {f"escalation:{first['request_id']}"}

    def test_only_a_live_answering_peer_of_the_same_ticket_that_can_receive_is_told(self) -> None:
        ticket_id = self.asker.ticket_id
        self.mailbox.live_peers = [
            _coordinator(ticket_id + 1000, address="other-ticket"),
            _coordinator(ticket_id, address="not-answering", phase="coding"),
            _coordinator(ticket_id, address="cannot-receive", can_receive=False),
            _coordinator(ticket_id, address=_ASKER),
            _coordinator(ticket_id, address="the-coordinator"),
        ]

        _call(self.mailbox)

        assert [to for to, *_ in self.mailbox.sent] == ["the-coordinator"]


class TestEscalatingWithoutALiveCoordinator(TestCase):
    def setUp(self) -> None:
        self.asker = _running()
        self.mailbox = FakeMailbox(_identity(self.asker))

    def test_the_threads_coordinator_task_is_resumed_with_the_fenced_request(self) -> None:
        parent = _answering_task(self.asker.ticket)

        receipt = _call(self.mailbox)

        row = DeferredQuestion.objects.get()
        assert receipt["delivery"] == "resumed"
        child = Task.objects.get(parent_task=parent)
        assert child.phase == "answering"
        assert f"escalation:{row.pk}" in child.execution_reason
        assert f"Escalation {row.pk} from planning task {self.asker.pk}" in child.execution_reason
        assert "<<<ESCALATION" in child.execution_reason
        assert self.mailbox.sent == []

    def test_a_peer_that_does_not_accept_in_time_leaves_the_request_to_the_resumed_task(self) -> None:
        self.mailbox.live_peers = [_coordinator(self.asker.ticket_id)]
        self.mailbox.outcome = "rejected"
        _answering_task(self.asker.ticket)

        receipt = _call(self.mailbox)

        assert receipt["delivery"] == "resumed"
        assert len(self.mailbox.sent) == 1

    def test_a_mailbox_that_fails_leaves_the_request_to_the_resumed_task(self) -> None:
        self.mailbox.broken = True
        _answering_task(self.asker.ticket)

        assert _call(self.mailbox)["delivery"] == "resumed"

    def test_nobody_to_tell_leaves_the_request_open_until_its_deadline(self) -> None:
        receipt = _call(self.mailbox)

        assert receipt == {
            "request_id": DeferredQuestion.objects.get().pk,
            "delivery": "unserved",
            "deadline_minutes": int(ESCALATION_DEADLINE.total_seconds() // 60),
        }
        assert DeferredQuestion.objects.get().is_pending


class TestWhoMayEscalate(TestCase):
    def test_the_answering_phase_is_refused_and_writes_nothing(self) -> None:
        asker = _running("answering")

        with pytest.raises(EscalationRefusedError, match="answering"):
            _call(FakeMailbox(_identity(asker)))

        assert not DeferredQuestion.objects.exists()

    def test_a_session_with_no_ticket_is_refused(self) -> None:
        asker = _running()
        identity = _identity(asker)
        del identity["ticket"]

        with pytest.raises(EscalationRefusedError, match="bound to a ticket"):
            _call(FakeMailbox(identity))

    def test_a_ticket_without_a_running_task_in_that_phase_is_refused(self) -> None:
        asker = _running()
        Task.objects.filter(pk=asker.pk).update(status=Task.Status.PENDING)

        with pytest.raises(EscalationRefusedError, match="cannot tell which running task"):
            _call(FakeMailbox(_identity(asker)))

    def test_a_running_task_of_another_phase_on_the_same_ticket_does_not_confuse_who_asks(self) -> None:
        asker = _running()
        TaskFactory.create(
            ticket=asker.ticket,
            session=SessionFactory.create(ticket=asker.ticket, agent_id="coding"),
            phase="coding",
            status=Task.Status.CLAIMED,
        )

        assert _call(FakeMailbox(_identity(asker)))["delivery"] == "unserved"
        assert DeferredQuestion.objects.get().parked_task_id == asker.pk

    def test_two_running_tasks_in_one_phase_cannot_be_told_apart(self) -> None:
        asker = _running()
        TaskFactory.create(
            ticket=asker.ticket,
            session=SessionFactory.create(ticket=asker.ticket, agent_id="planning"),
            phase="planning",
            status=Task.Status.CLAIMED,
        )

        with pytest.raises(EscalationRefusedError, match="cannot tell which running task"):
            _call(FakeMailbox(_identity(asker)))
        assert not DeferredQuestion.objects.exists()

    def test_an_unknown_kind_is_refused_before_anything_is_sent(self) -> None:
        asker = _running()
        mailbox = FakeMailbox(_identity(asker), [_coordinator(asker.ticket_id)])

        with pytest.raises(EscalationRefusedError, match="valid kinds"):
            _call(mailbox, kind="spend_money")

        assert mailbox.sent == []


def test_the_tool_name_is_the_one_the_phase_table_grants() -> None:
    assert ESCALATE in mcp_write_tools_for_phase("planning")
