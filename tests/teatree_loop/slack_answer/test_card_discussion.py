"""A free-text card reply reaches the responsible agent on the direct mailbox."""

# test-path: cross-cutting

import asyncio
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase

from teatree.agents import live_mailbox
from teatree.agents.live_mailbox_client import MailboxClient
from teatree.core.models import DeferredQuestion, PendingChatInjection, Session, Task
from tests._owner_channel import OWNER_CARD, OWNER_SLACK_ID
from tests.factories import planned_ticket
from tests.teatree_loop.slack_answer.test_thread_card_replies import Backend, _action_card, _card, _cycle, _event


class TestCardDiscussion(TestCase):
    def _reply_with_peer(
        self, *, responsible: bool, action: bool = False
    ) -> tuple[str, DeferredQuestion, PendingChatInjection, Backend]:
        with (
            tempfile.TemporaryDirectory(prefix="t3-card-", dir="/tmp") as directory,
            live_mailbox.LiveMailboxBroker(runtime_dir=Path(directory)) as broker,
        ):
            card = _action_card("335.1") if action else _card("335.1")
            ticket = planned_ticket(issue_url="https://github.com/acme/widgets/issues/42")
            task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
            if responsible:
                card.parked_task = task
                card.save(update_fields=["parked_task"])
            peer = broker.register(
                room=str(ticket.pk) if responsible else "",
                harness="claude_sdk" if responsible else "interactive",
                label=f"coding task {task.pk}" if responsible else "owner coordinator",
            )
            backend = Backend(user_id=OWNER_SLACK_ID)
            backend.message_meta["335.2"] = {"ts": "335.2", "thread_ts": "335.1"}
            reply = _event(backend, ts="335.2", root="335.1", text="Show me the rule")
            assert reply is not None
            with ThreadPoolExecutor(max_workers=1) as executor:
                received = executor.submit(
                    lambda: asyncio.run(MailboxClient(peer.socket_path, peer.token).wait(timeout_seconds=3))
                )
                with patch.object(live_mailbox, "shared_broker", return_value=broker):
                    _cycle(backend)
                page = received.result(timeout=5)
            card.refresh_from_db()
            reply.refresh_from_db()
            return page["messages"][0]["text"], card, reply, backend

    def test_responsible_agent_receives_subject_and_thread_without_closing_card(self) -> None:
        message, card, reply, backend = self._reply_with_peer(responsible=True)

        assert "Show me the rule" in message
        assert "https://github.com/acme/widgets/issues/42" in message
        assert "335.1" in message
        assert card.is_pending
        assert reply.answer_kind == "delegated"
        assert not backend.replies

    def test_coordinator_receives_when_no_agent_is_responsible(self) -> None:
        message, card, reply, backend = self._reply_with_peer(responsible=False)

        assert "Show me the rule" in message
        assert "Approve the change?" in message
        assert card.is_pending
        assert reply.answer_kind == "delegated"
        assert not backend.replies

    def test_coordinator_reply_uses_the_same_thread_and_keeps_the_card_open(self) -> None:
        message, card, reply, _backend = self._reply_with_peer(responsible=False)
        notifier = MagicMock()
        notifier.open_dm.return_value = "D1"
        notifier.post_message.return_value = {"ok": True, "ts": "335.3"}
        notifier.get_permalink.return_value = "https://acme.slack.com/archives/D1/p3353"

        with patch("teatree.core.notify.messaging_from_overlay", return_value=notifier):
            call_command(
                "notify",
                "reply",
                "The current rule says to decide settled questions.",
                "--user-id",
                OWNER_SLACK_ID,
                "--idempotency-key",
                f"card-discussion:{reply.slack_ts}:reply",
                "--thread-ts",
                reply.thread_ts,
            )

        assert "notify reply" in message
        assert notifier.post_message.call_args.kwargs["thread_ts"] == "335.1"
        card.refresh_from_db()
        assert card.is_pending

    def test_action_card_discussion_reaches_agent_without_accepting_action(self) -> None:
        message, card, reply, backend = self._reply_with_peer(responsible=True, action=True)

        assert "Show me the rule" in message
        assert card.is_pending
        assert reply.answer_kind == "delegated"
        assert not backend.replies

    def test_unreachable_responsible_agent_gets_a_durable_followup(self) -> None:
        card = _card("340.1")
        ticket = planned_ticket(issue_url="https://github.com/acme/widgets/issues/43")
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        card.parked_task = task
        card.save(update_fields=["parked_task"])
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.message_meta["340.2"] = {"ts": "340.2", "thread_ts": "340.1"}
        reply = _event(backend, ts="340.2", root="340.1", text="Show me the rule")
        assert reply is not None

        _cycle(backend)

        card.refresh_from_db()
        reply.refresh_from_db()
        child = task.child_tasks.get()
        assert "Show me the rule" in child.execution_reason
        assert "340.1" in child.execution_reason
        assert card.is_pending
        assert reply.answer_kind == "delegated"

    def test_card_without_options_still_routes_discussion_to_coordinator(self) -> None:
        card = DeferredQuestion.record(
            "Which dependency should we use?",
            card=replace(OWNER_CARD, options=()),
            slack_channel="D1",
            slack_ts="345.1",
        )
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.message_meta["345.2"] = {"ts": "345.2", "thread_ts": "345.1"}
        reply = _event(backend, ts="345.2", root="345.1", text="Show me the rule")
        assert reply is not None
        with (
            tempfile.TemporaryDirectory(prefix="t3-card-", dir="/tmp") as directory,
            live_mailbox.LiveMailboxBroker(runtime_dir=Path(directory)) as broker,
        ):
            peer = broker.register(room="", harness="interactive", label="owner coordinator")
            with ThreadPoolExecutor(max_workers=1) as executor:
                received = executor.submit(
                    lambda: asyncio.run(MailboxClient(peer.socket_path, peer.token).wait(timeout_seconds=3))
                )
                with patch.object(live_mailbox, "shared_broker", return_value=broker):
                    _cycle(backend)
                page = received.result(timeout=5)

        assert "Show me the rule" in page["messages"][0]["text"]
        assert DeferredQuestion.objects.get(pk=card.pk).is_pending
