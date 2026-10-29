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
from teatree.core.models import DeferredQuestion, PendingChatInjection, Session, Task, Ticket
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

    def test_peer_that_does_not_accept_gets_a_durable_followup(self) -> None:
        card = _card("341.1")
        ticket = planned_ticket(issue_url="https://github.com/acme/widgets/issues/44")
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        card.parked_task = task
        card.save(update_fields=["parked_task"])
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.message_meta["341.2"] = {"ts": "341.2", "thread_ts": "341.1"}
        reply = _event(backend, ts="341.2", root="341.1", text="Show me the rule")
        assert reply is not None

        with patch("teatree.loop.slack_answer.card_discussion._send", return_value=(False, True)):
            _cycle(backend)

        assert task.child_tasks.count() == 1
        assert "Show me the rule" in task.child_tasks.get().execution_reason
        assert DeferredQuestion.objects.get(pk=card.pk).is_pending

    def test_routing_error_releases_the_inbound_reply_for_retry(self) -> None:
        card = _card("342.1")
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.message_meta["342.2"] = {"ts": "342.2", "thread_ts": "342.1"}
        reply = _event(backend, ts="342.2", root="342.1", text="Show me the rule")
        assert reply is not None

        with patch(
            "teatree.loop.slack_answer.card_discussion.route_card_discussion",
            side_effect=RuntimeError("db unavailable"),
        ):
            report = _cycle(backend)

        reply.refresh_from_db()
        assert card.is_pending
        assert report.errors == 1
        assert reply.loop_replied_at is None

    def test_nobody_to_answer_gets_one_plain_line_and_the_message_is_kept(self) -> None:
        card = _card("346.1")
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.message_meta["346.2"] = {"ts": "346.2", "thread_ts": "346.1"}
        reply = _event(backend, ts="346.2", root="346.1", text="Show me the rule")
        assert reply is not None

        with (
            tempfile.TemporaryDirectory(prefix="t3-card-", dir="/tmp") as directory,
            live_mailbox.LiveMailboxBroker(runtime_dir=Path(directory)) as broker,
            patch.object(live_mailbox, "shared_broker", return_value=broker),
        ):
            reports = [_cycle(backend) for _ in range(3)]
            reply.refresh_from_db()
            kept_for_retry = reply.loop_replied_at is None
            peer = broker.register(room="", harness="interactive", label="owner coordinator")
            with ThreadPoolExecutor(max_workers=1) as executor:
                received = executor.submit(
                    lambda: asyncio.run(MailboxClient(peer.socket_path, peer.token).wait(timeout_seconds=3))
                )
                _cycle(backend)
                page = received.result(timeout=5)

        reply.refresh_from_db()
        (_channel, answered_ts, line) = backend.replies[0]
        assert len(backend.replies) == 1
        assert answered_ts == "346.2"
        assert "no coordinator session is connected" in line
        assert "The card stays open." in line
        assert [report.dispatched for report in reports] == [0, 0, 0]
        assert kept_for_retry
        assert "Show me the rule" in page["messages"][0]["text"]
        assert reply.answer_kind == "delegated"
        assert DeferredQuestion.objects.get(pk=card.pk).is_pending

    def test_unrouted_card_replies_do_not_starve_new_card_and_direct_message(self) -> None:
        backend = Backend(user_id=OWNER_SLACK_ID)
        for index in range(10):
            root = f"360.{index * 2 + 1:04d}"
            ts = f"360.{index * 2 + 2:04d}"
            _card(root)
            backend.message_meta[ts] = {"ts": ts, "thread_ts": root}
            assert _event(backend, ts=ts, root=root, text="Show me the rule") is not None

        with (
            tempfile.TemporaryDirectory(prefix="t3-card-", dir="/tmp") as directory,
            live_mailbox.LiveMailboxBroker(runtime_dir=Path(directory)) as broker,
            patch.object(live_mailbox, "shared_broker", return_value=broker),
        ):
            _cycle(backend)
            assert DeferredQuestion.objects.filter(audits__action="reply_unrouted").count() == 10
            newer = _card("361.1")
            option_reply = _event(backend, ts="361.2", root="361.1", text="approve")
            direct_message = _event(backend, ts="361.3", root="", text="Please inspect the build")
            assert option_reply is not None
            assert direct_message is not None
            reports = [_cycle(backend) for _ in range(4)]

        newer.refresh_from_db()
        direct_message.refresh_from_db()
        assert newer.answer_text == "Approve"
        assert direct_message.answer_kind == PendingChatInjection.AnswerKind.DELEGATED
        assert sum(report.answered_question for report in reports) == 1
        assert sum(report.dispatched for report in reports) == 1
        assert len(backend.replies) == 10

    def test_the_plain_line_is_not_repeated_when_the_thread_cannot_be_read_back(self) -> None:
        _card("348.1")
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.message_meta["348.2"] = {"ts": "348.2", "thread_ts": "348.1"}
        assert _event(backend, ts="348.2", root="348.1", text="Show me the rule") is not None
        backend.read_after_post = False

        with (
            tempfile.TemporaryDirectory(prefix="t3-card-", dir="/tmp") as directory,
            live_mailbox.LiveMailboxBroker(runtime_dir=Path(directory)) as broker,
            patch.object(live_mailbox, "shared_broker", return_value=broker),
        ):
            for _ in range(3):
                _cycle(backend)

        assert len(backend.replies) == 1

    def test_responsible_task_that_cannot_resume_still_gets_a_plain_line(self) -> None:
        card = _card("347.1")
        ticket = Ticket.objects.create()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        card.parked_task = task
        card.save(update_fields=["parked_task"])
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.message_meta["347.2"] = {"ts": "347.2", "thread_ts": "347.1"}
        assert _event(backend, ts="347.2", root="347.1", text="Show me the rule") is not None

        with (
            tempfile.TemporaryDirectory(prefix="t3-card-", dir="/tmp") as directory,
            live_mailbox.LiveMailboxBroker(runtime_dir=Path(directory)) as broker,
            patch.object(live_mailbox, "shared_broker", return_value=broker),
        ):
            _cycle(backend)
            _cycle(backend)

        assert len(backend.replies) == 1
        assert "No agent is working on this card" in backend.replies[0][2]
        assert not task.child_tasks.exists()
        assert DeferredQuestion.objects.get(pk=card.pk).is_pending

    def test_unaccepted_mailbox_retry_does_not_enqueue_the_message_twice(self) -> None:
        card = _card("343.1")
        backend = Backend(user_id=OWNER_SLACK_ID)
        backend.message_meta["343.2"] = {"ts": "343.2", "thread_ts": "343.1"}
        reply = _event(backend, ts="343.2", root="343.1", text="Show me the rule")
        assert reply is not None

        with (
            tempfile.TemporaryDirectory(prefix="t3-card-", dir="/tmp") as directory,
            live_mailbox.LiveMailboxBroker(runtime_dir=Path(directory)) as broker,
        ):
            peer = broker.register(room="", harness="interactive", label="owner coordinator")
            with patch.object(live_mailbox, "shared_broker", return_value=broker):
                _cycle(backend)
                _cycle(backend)
            messages = asyncio.run(MailboxClient(peer.socket_path, peer.token).inbox())["messages"]

        assert len(messages) == 1
        assert DeferredQuestion.objects.get(pk=card.pk).is_pending

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
