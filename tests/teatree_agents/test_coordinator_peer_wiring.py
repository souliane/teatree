"""The recorder hands message_peer to the live mailbox, so the agent on the named ticket receives it (#5685)."""

import asyncio
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from teatree.agents import live_mailbox
from teatree.agents.live_control import LiveTask
from teatree.agents.live_mailbox_client import MailboxClient
from teatree.agents.reactive_envelope_recorders import record_reactive_envelopes
from teatree.core.answering.escalation import (
    ESCALATION_DEADLINE,
    INTERNAL_KIND,
    open_request,
    record_request,
    settle_expired_escalations,
)
from teatree.core.models import DeferredQuestion, Session, Task, ThreadBinding, Ticket
from teatree.types import RawAPIDict
from teatree.utils.url_slug import slack_conversation_anchor
from tests.factories import SessionFactory, TaskFactory

_WORK_URL = "https://github.com/souliane/teatree/issues/4084"


@dataclass
class _Backend:
    replies: list[tuple[str, str, str]] = field(default_factory=list)

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        self.replies.append((channel, ts, text))
        return {"ok": True}


class TestTheRecorderReachesTheLiveMailbox(TestCase):
    def test_a_message_peer_proposal_reaches_the_agent_on_the_named_ticket(self) -> None:
        backend = _Backend()
        work = Ticket.objects.create(overlay="t3-teatree", issue_url=_WORK_URL, short_description="Fix rounding")
        ticket = Ticket.objects.create(
            issue_url=slack_conversation_anchor(channel="D-owner", slack_ts="1.0"),
            overlay="t3-teatree",
            extra={"slack_answer": {"channel": "D-owner", "slack_ts": "1.0", "question": f"tell {_WORK_URL} to use B"}},
        )
        ThreadBinding.bind(ticket=ticket, channel="D-owner", thread_ts="1.0")
        task = Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket, overlay="t3-teatree"), phase="answering"
        )
        result = {
            "answer": {"text": "Done."},
            "actions": [{"kind": "message_peer", "ticket": _WORK_URL, "text": "Use approach B."}],
        }

        with (
            mock.patch("teatree.core.backend_factory.messaging_from_overlay", return_value=backend),
            tempfile.TemporaryDirectory(prefix="t3-wire-", dir="/tmp") as directory,
            live_mailbox.LiveMailboxBroker(runtime_dir=Path(directory)) as broker,
            mock.patch.object(live_mailbox, "shared_broker", return_value=broker),
        ):
            agent = broker.register(room=str(work.pk), harness="claude_sdk", label="coding task 7")
            with ThreadPoolExecutor(max_workers=1) as pool:
                reading = pool.submit(
                    lambda: asyncio.run(MailboxClient(agent.socket_path, agent.token).wait(timeout_seconds=3))
                )
                record_reactive_envelopes(task, result, phase="answering")
                received = [message["text"] for message in reading.result(timeout=5)["messages"]]

        assert received == ["Use approach B."]
        assert f"Sent your message to the agent working on <{_WORK_URL}|#4084>." in backend.replies[0][2]


class TestTheCoordinatorsReplyReachesTheAskingTask(TestCase):
    def test_a_reply_returned_on_the_askers_ticket_reaches_the_asker_and_ends_the_request(self) -> None:
        backend = _Backend()
        work = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url=_WORK_URL,
            short_description="Fix rounding",
            extra={"slack_answer": {"channel": "D-owner", "slack_ts": "1.0", "question": "how is it going"}},
        )
        asker = TaskFactory.create(
            ticket=work,
            session=SessionFactory.create(ticket=work, agent_id="planning"),
            phase="planning",
            status=Task.Status.CLAIMED,
        )
        request = record_request(asker, kind=INTERNAL_KIND, message="Which order should I follow")
        coordinator = Task.objects.create(
            ticket=work, session=Session.objects.create(ticket=work, overlay="t3-teatree"), phase="answering"
        )
        result = {
            "answer": {"text": "Replied."},
            "actions": [{"kind": "message_peer", "ticket": _WORK_URL, "text": "Follow order B."}],
        }

        with (
            mock.patch("teatree.core.backend_factory.messaging_from_overlay", return_value=backend),
            tempfile.TemporaryDirectory(prefix="t3-wire-", dir="/tmp") as directory,
            live_mailbox.LiveMailboxBroker(runtime_dir=Path(directory)) as broker,
            mock.patch.object(live_mailbox, "shared_broker", return_value=broker),
        ):
            reader = broker.register(room=str(work.pk), harness="claude_sdk", label="planning task")
            answering = broker.register(
                room=str(work.pk),
                harness="claude_sdk",
                label="answering task",
                task=LiveTask(pk=coordinator.pk, ticket=work.pk, phase="answering", harness="claude_sdk", model="m"),
            )
            with ThreadPoolExecutor(max_workers=1) as pool:
                reading = pool.submit(
                    lambda: asyncio.run(MailboxClient(reader.socket_path, reader.token).wait(timeout_seconds=3))
                )
                record_reactive_envelopes(coordinator, result, phase="answering")
                received = [message["text"] for message in reading.result(timeout=5)["messages"]]
            answering_inbox = asyncio.run(MailboxClient(answering.socket_path, answering.token).inbox())["messages"]

        assert received == ["Follow order B."]
        assert answering_inbox == []
        assert f"Sent your message to the agent working on <{_WORK_URL}|#4084>." in backend.replies[0][2]
        assert open_request(work.pk) is None
        DeferredQuestion.objects.filter(pk=request.pk).update(
            created_at=timezone.now() - ESCALATION_DEADLINE - timedelta(minutes=1)
        )
        assert settle_expired_escalations() == 0
        asker.refresh_from_db()
        assert asker.status == Task.Status.CLAIMED
        assert not DeferredQuestion.owner_pending().exists()
