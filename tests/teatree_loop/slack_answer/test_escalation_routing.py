"""While an escalation is open, the owner's next thread message goes to the coordinator, then routes back (#5685)."""

import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from teatree.agents import live_mailbox
from teatree.agents.live_registry import LiveSessionRegistry
from teatree.core.answering.escalation import ESCALATION_DEADLINE, record_request
from teatree.core.models import DmContext, PendingChatInjection, Session, SessionCommand, Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.inbound_reading import InboundIntent, InboundReading, ReadingSource
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle
from teatree.loop.slack_answer.orchestration import WorkOrigin, dispatch_work
from tests._escalation_peers import AcceptingSession, live_task_of
from tests.teatree_agents._live_owner import OwnerLoop
from tests.teatree_loop.slack_answer.test_cycle import _USER_UID, RecordingBackend, _resolver

_OWNER_TEXT = "Use the second option for the backfill"


class _Thread(TestCase):
    """A bound Slack thread whose ticket has a finished answering task and a running planning task."""

    def setUp(self) -> None:
        reading = InboundReading(
            intent=InboundIntent.INSTRUCTION, answerable=False, work_summary="build feature", source=ReadingSource.MODEL
        )
        self.coordinator = dispatch_work(
            reading=reading,
            fingerprint="bound-feature",
            origin=WorkOrigin("", "C1", "100.0", ("100.0",), "build feature"),
        )
        Task.objects.filter(pk=self.coordinator.pk).update(status=Task.Status.COMPLETED)
        self.ticket = self.coordinator.ticket
        self.asker = Task.objects.create(
            ticket=self.ticket,
            session=Session.objects.create(ticket=self.ticket),
            phase="planning",
            status=Task.Status.CLAIMED,
        )
        self.backend = RecordingBackend()
        self.client = Mock(unreachable={})
        self.client.sessions.return_value = [
            {"task": self.asker.pk, "ticket": self.ticket.pk, "state": "busy", "steerable": True}
        ]
        self.client.steer.return_value = {"outcome": "accepted_current_turn", "code": None, "accepted_at": "now"}

    def _owner_message(self, text: str = _OWNER_TEXT, ts: str = "101.0") -> PendingChatInjection:
        row = PendingChatInjection.record(
            channel="C1", slack_ts=ts, text=text, context=DmContext(user_id=_USER_UID, thread_ts="100.0")
        )
        assert row is not None
        self.backend.message_meta[ts] = {"ts": ts, "thread_ts": "100.0"}
        return row

    def _cycle(self):
        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=self.client):
            return run_slack_answer_cycle(messaging_resolver=_resolver(self.backend))

    def _escalate(self) -> DeferredQuestion:
        return record_request(self.asker, kind="internal", message="Which backfill order?")


class TestAnOpenEscalationRoutesToTheCoordinator(_Thread):
    def test_the_owner_message_resumes_the_coordinator_task_and_does_not_steer_the_asker(self) -> None:
        self._escalate()
        row = self._owner_message()

        report = self._cycle()

        assert report.dispatched == 1
        self.client.steer.assert_not_called()
        child = Task.objects.get(parent_task=self.coordinator)
        assert child.phase == "answering"
        assert _OWNER_TEXT in child.execution_reason
        assert "OWNER MESSAGE" in child.execution_reason
        row.refresh_from_db()
        assert row.answer_kind == PendingChatInjection.AnswerKind.DELEGATED
        assert not SessionCommand.objects.filter(task=self.asker).exists()

    def test_a_live_coordinator_session_receives_it_instead(self) -> None:
        self._escalate()
        self._owner_message()
        registry = LiveSessionRegistry()
        live_coordinator = Task.objects.create(
            ticket=self.ticket, session=Session.objects.create(ticket=self.ticket), phase="answering"
        )
        live = live_task_of(live_coordinator)
        session = AcceptingSession()
        with (
            tempfile.TemporaryDirectory(prefix="t3-esc-", dir="/tmp") as directory,
            live_mailbox.LiveMailboxBroker(runtime_dir=Path(directory), registry=lambda: registry) as broker,
            patch.object(live_mailbox, "shared_broker", return_value=broker),
            OwnerLoop(registry, live, session),
        ):
            broker.register(
                room=str(self.ticket.pk), harness="claude_sdk", label=f"answering task {live.pk}", task=live
            )
            self._cycle()

        self.client.steer.assert_not_called()
        assert _OWNER_TEXT in session.delivered[0]
        assert not Task.objects.filter(parent_task=self.coordinator).exists()

    def test_the_message_reaches_the_asker_when_nobody_can_be_told(self) -> None:
        Task.objects.filter(pk=self.coordinator.pk).update(phase="planning")
        self._escalate()
        row = self._owner_message()

        self._cycle()

        self.client.steer.assert_called_once()
        row.refresh_from_db()
        assert row.answer_kind == PendingChatInjection.AnswerKind.SESSION

    def test_pause_still_reaches_the_task_while_a_request_is_open(self) -> None:
        self._escalate()
        self._owner_message(text="pause")
        self.client.pause.return_value = {"outcome": "accepted_current_turn", "code": None, "accepted_at": "now"}

        self._cycle()

        self.client.pause.assert_called_once()
        assert not Task.objects.filter(parent_task=self.coordinator).exists()


class TestTheMessageRoutesBack(_Thread):
    def test_once_the_request_is_answered_the_message_steers_the_asker_again(self) -> None:
        request = self._escalate()
        assert request.apply_answer("go with the second", resolved_via="agent") is not None
        self._owner_message()

        self._cycle()

        self.client.steer.assert_called_once()
        assert not Task.objects.filter(parent_task=self.coordinator).exists()

    def test_once_the_request_expired_the_message_steers_the_asker_again(self) -> None:
        request = self._escalate()
        DeferredQuestion.objects.filter(pk=request.pk).update(
            created_at=timezone.now() - ESCALATION_DEADLINE - timedelta(minutes=1)
        )
        self._owner_message()

        self._cycle()

        self.client.steer.assert_called_once()

    def test_a_request_on_another_ticket_does_not_take_this_thread(self) -> None:
        elsewhere = Ticket.objects.create(overlay="t3-teatree", issue_url="https://github.com/acme/widgets/issues/5")
        other = Task.objects.create(
            ticket=elsewhere,
            session=Session.objects.create(ticket=elsewhere),
            phase="planning",
            status=Task.Status.CLAIMED,
        )
        record_request(other, kind="internal", message="Elsewhere?")
        self._owner_message()

        self._cycle()

        self.client.steer.assert_called_once()
        assert not Task.objects.filter(parent_task=self.coordinator).exists()
