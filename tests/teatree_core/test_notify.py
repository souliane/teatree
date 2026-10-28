"""Behaviour tests for the bot→user notification helper (#963).

These exercise the TRANSPORT — threading, linkify, idempotency, failure audit — so each
needs a notification that actually reaches it. Since #4524 the push/pull classifier keeps
an unregistered status signal off the DM channel entirely, and a question is the one thing
that always reaches it whatever its key; the routing decision itself is covered by
``test_dm_channel_policy.py`` and ``test_notify_pull_routing.py``.
"""

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.db import OperationalError, connection
from django.test import TestCase

from teatree.core import notify as notify_module
from teatree.core.backend_protocols import MessagingReadRefusedError
from teatree.core.egress_transport import EgressKind, suppress_on_behalf_egress
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import BotPing, IncomingEvent, OperatorIdentity, Ticket, TicketDmPost, TicketDmThread
from teatree.core.notify import NotifyKind, notify_user, notify_user_outcome, resolve_owner_dm_backend
from teatree.core.notify_types import NotifyOptions, NotifyReason
from teatree.core.send_proxy import SendVerdict
from teatree.types import RawAPIDict

_DB_LOCKED = OperationalError("database is locked")


def _seed_config(db: Path, key: str, value: object, scope: str = "") -> None:
    """Seed a ``teatree_config_setting`` row the cold reader (and load_config) resolve."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES (?, ?, ?)",
            (scope, key, json.dumps(value)),
        )
        conn.commit()
    finally:
        conn.close()


def _backend(*, permalink: str = "https://acme.slack.com/archives/D-USER/p1700000000000000") -> MagicMock:
    b = MagicMock()
    b.open_dm.return_value = "D-USER"
    b.post_message.return_value = {"ok": True, "ts": "1700000000.000000"}
    b.get_permalink.return_value = permalink
    return b


class TestNotifyUser(TestCase):
    def test_ticket_question_goes_to_registered_requester(self) -> None:
        user = get_user_model().objects.create_superuser(username="alice", email="", password="unused")
        OperatorIdentity.objects.create(user=user, platform="github", handle="alice", external_id="123")
        OperatorIdentity.objects.create(user=user, platform="slack", handle="U-OP")
        ticket = Ticket.objects.create(issue_url="https://github.com/acme/app/issues/44", requested_by="github:alice")
        backend = _backend()

        outcome = notify_user_outcome(
            "question",
            kind=NotifyKind.QUESTION,
            idempotency_key="operator-question",
            audience=NotifyAudience.OWNER_QUESTION,
            options=NotifyOptions(backend=backend, user_id="U_ME", ticket_id=ticket.pk),
        )

        assert outcome.sent
        backend.open_dm.assert_called_once_with("U-OP")

    def test_failed_requester_delivery_does_not_retry_to_owner(self) -> None:
        user = get_user_model().objects.create_superuser(username="alice", email="", password="unused")
        OperatorIdentity.objects.create(user=user, platform="github", handle="alice", external_id="123")
        OperatorIdentity.objects.create(user=user, platform="slack", handle="U-OP")
        ticket = Ticket.objects.create(issue_url="https://github.com/acme/app/issues/45", requested_by="github:alice")
        backend = _backend()
        backend.open_dm.return_value = ""

        outcome = notify_user_outcome(
            "question",
            kind=NotifyKind.QUESTION,
            idempotency_key="failed-operator-question",
            audience=NotifyAudience.OWNER_QUESTION,
            options=NotifyOptions(backend=backend, user_id="U_ME", ticket_id=ticket.pk),
        )

        assert outcome.reason is NotifyReason.DELIVERY_FAILED
        backend.open_dm.assert_called_once_with("U-OP")

    def test_send_proxy_self_dm_carveout_only_covers_owner(self) -> None:
        user = get_user_model().objects.create_superuser(username="alice", email="", password="unused")
        OperatorIdentity.objects.create(user=user, platform="github", handle="alice", external_id="123")
        OperatorIdentity.objects.create(user=user, platform="slack", handle="U-OP")
        operator_ticket = Ticket.objects.create(requested_by="github:alice")
        owner_ticket = Ticket.objects.create()
        with patch("teatree.core.notify.route_send") as route:
            for ticket, key in ((operator_ticket, "operator-audit"), (owner_ticket, "owner-audit")):
                notify_user_outcome(
                    "question",
                    kind=NotifyKind.QUESTION,
                    idempotency_key=key,
                    audience=NotifyAudience.OWNER_QUESTION,
                    options=NotifyOptions(backend=_backend(), user_id="U_ME", ticket_id=ticket.pk),
                )

        assert [(call.args[0].destination, call.args[0].is_self_dm) for call in route.call_args_list] == [
            ("U-OP", False),
            ("U_ME", True),
        ]

    def test_operator_dm_refuses_a_send_proxy_denial(self) -> None:
        user = get_user_model().objects.create_superuser(username="alice", email="", password="unused")
        OperatorIdentity.objects.create(user=user, platform="github", handle="alice", external_id="123")
        OperatorIdentity.objects.create(user=user, platform="slack", handle="U-OP")
        ticket = Ticket.objects.create(requested_by="github:alice")
        backend = _backend()
        with patch(
            "teatree.core.notify.route_send",
            return_value=SendVerdict(allowed=False, payload="question", allowlist_ok=False, reason="denied"),
        ):
            outcome = notify_user_outcome(
                "question",
                kind=NotifyKind.QUESTION,
                idempotency_key="operator-denied",
                audience=NotifyAudience.OWNER_QUESTION,
                options=NotifyOptions(backend=backend, user_id="U_ME", ticket_id=ticket.pk),
            )
        assert outcome.reason is NotifyReason.DELIVERY_FAILED
        backend.post_message.assert_not_called()

    def test_operator_dm_refuses_an_unreadable_redaction_policy(self) -> None:
        user = get_user_model().objects.create_superuser(username="alice", email="", password="unused")
        OperatorIdentity.objects.create(user=user, platform="github", handle="alice", external_id="123")
        OperatorIdentity.objects.create(user=user, platform="slack", handle="U-OP")
        ticket = Ticket.objects.create(requested_by="github:alice")
        backend = _backend()
        with patch(
            "teatree.core.notify.route_send",
            return_value=SendVerdict(allowed=True, payload="question", allowlist_ok=True, redaction_failed=True),
        ):
            outcome = notify_user_outcome(
                "question",
                kind=NotifyKind.QUESTION,
                idempotency_key="operator-policy-unreadable",
                audience=NotifyAudience.OWNER_QUESTION,
                options=NotifyOptions(backend=backend, user_id="U_ME", ticket_id=ticket.pk),
            )
        assert outcome.reason is NotifyReason.DELIVERY_FAILED
        backend.post_message.assert_not_called()

    def test_requester_without_chat_identity_goes_to_owner_with_label(self) -> None:
        user = get_user_model().objects.create_superuser(username="alice", email="", password="unused")
        OperatorIdentity.objects.create(user=user, platform="github", handle="alice", external_id="123")
        ticket = Ticket.objects.create(issue_url="https://github.com/acme/app/issues/46", requested_by="github:alice")
        backend = _backend()

        outcome = notify_user_outcome(
            "question",
            kind=NotifyKind.QUESTION,
            idempotency_key="fallback-operator-question",
            audience=NotifyAudience.OWNER_QUESTION,
            options=NotifyOptions(backend=backend, user_id="U_ME", ticket_id=ticket.pk),
        )

        assert outcome.sent
        backend.open_dm.assert_called_once_with("U_ME")
        assert "github:alice" in str(backend.post_message.call_args)

    def test_ticket_notes_share_one_header_per_ticket(self) -> None:
        tickets = [
            Ticket.objects.create(
                overlay="teatree",
                issue_url=f"https://github.com/example/project/issues/{number}",
                short_description=f"Issue {number}",
            )
            for number in (12, 13)
        ]
        backend = _backend()
        posted: list[tuple[str, str, str]] = []

        def post_message(*, channel: str, text: str, thread_ts: str = "", **_kwargs: object) -> RawAPIDict:
            ts = f"1700000000.{len(posted) + 1:06d}"
            posted.append((text, thread_ts, ts))
            return {"ok": True, "ts": ts}

        backend.post_message.side_effect = post_message
        backend.fetch_thread_replies.side_effect = lambda *, channel, thread_ts: [
            {"ts": thread_ts, "text": next(text for text, _, ts in posted if ts == thread_ts)}
        ]

        for index, ticket in enumerate((tickets[0], tickets[1], tickets[0])):
            outcome = notify_user_outcome(
                f"note {index}",
                kind=NotifyKind.INFO,
                idempotency_key=f"ticket-note-{index}",
                audience=NotifyAudience.OWNER_QUESTION,
                options=NotifyOptions(backend=backend, user_id="U_ME", ticket_id=ticket.pk),
            )
            assert outcome.sent

        roots = [entry for entry in posted if not entry[1]]
        assert len(roots) == 2
        assert roots[0][0] == "<https://github.com/example/project/issues/12|example/project#12> Issue 12"
        assert roots[1][0] == "<https://github.com/example/project/issues/13|example/project#13> Issue 13"
        assert [entry[1] for entry in posted if "note " in entry[0]] == [
            roots[0][2],
            roots[1][2],
            roots[0][2],
        ]
        assert TicketDmPost.objects.count() == 5

    def test_failed_ticket_header_never_posts_body_at_root(self) -> None:
        ticket = Ticket.objects.create(overlay="teatree", short_description="Retry")
        backend = _backend()
        backend.post_message.return_value = {"ok": False, "error": "ratelimited"}

        outcome = notify_user_outcome(
            "body",
            kind=NotifyKind.INFO,
            idempotency_key="failed-header",
            audience=NotifyAudience.OWNER_QUESTION,
            options=NotifyOptions(backend=backend, user_id="U_ME", ticket_id=ticket.pk),
        )

        assert not outcome.sent
        assert "ticket header failed" in outcome.detail
        assert backend.post_message.call_count == 1
        thread = TicketDmThread.objects.get(ticket=ticket)
        assert (thread.root_ts, thread.generation) == ("", 0)

    def test_proven_deleted_root_reopens_on_next_message(self) -> None:
        ticket = Ticket.objects.create(overlay="teatree", short_description="Retry")
        backend = _backend()
        posted: list[tuple[str, str, str]] = []
        refusal = ""

        def post_message(*, channel: str, text: str, thread_ts: str = "", **_kwargs: object) -> RawAPIDict:
            nonlocal refusal
            if "second" in text:
                refusal = "thread_not_found"
                return {"ok": False, "error": "ratelimited"}
            ts = f"1700000000.{len(posted) + 1:06d}"
            posted.append((text, thread_ts, ts))
            return {"ok": True, "ts": ts}

        def fetch(*, channel: str, thread_ts: str) -> list[RawAPIDict]:
            if refusal:
                method = "conversations.replies"
                raise MessagingReadRefusedError(method, thread_ts, refusal)
            return [{"ts": thread_ts, "text": "header"}]

        backend.post_message.side_effect = post_message
        backend.fetch_thread_replies.side_effect = fetch

        def send(body: str) -> bool:
            return notify_user_outcome(
                body,
                kind=NotifyKind.INFO,
                idempotency_key=f"deleted-root-{body}",
                audience=NotifyAudience.OWNER_QUESTION,
                options=NotifyOptions(backend=backend, user_id="U_ME", ticket_id=ticket.pk),
            ).sent

        assert send("first")
        first_root = TicketDmThread.objects.get(ticket=ticket).root_ts
        assert not send("second")
        thread = TicketDmThread.objects.get(ticket=ticket)
        assert (thread.root_ts, thread.generation) == ("", 1)
        refusal = ""
        assert send("third")
        thread.refresh_from_db()
        assert thread.root_ts != first_root
        assert thread.generation == 1
        assert len([entry for entry in posted if not entry[1]]) == 2

    def test_tombstoned_root_reopens_after_refusing_the_current_message(self) -> None:
        ticket = Ticket.objects.create(short_description="Tombstone")
        backend = _backend()
        posts: list[tuple[str, str]] = []
        tombstoned = False

        def post_message(*, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
            posts.append((text, thread_ts))
            return {"ok": True, "ts": f"1700000000.{len(posts):06d}"}

        def fetch(*, channel: str, thread_ts: str) -> list[RawAPIDict]:
            return [{"ts": thread_ts, "subtype": "tombstone" if tombstoned else ""}]

        backend.post_message.side_effect = post_message
        backend.fetch_thread_replies.side_effect = fetch

        def send(body: str) -> bool:
            return notify_user_outcome(
                body,
                kind=NotifyKind.INFO,
                idempotency_key=f"tombstoned-{body}",
                audience=NotifyAudience.OWNER_QUESTION,
                options=NotifyOptions(backend=backend, user_id="U_ME", ticket_id=ticket.pk),
            ).sent

        assert send("first")
        tombstoned = True
        assert not send("second")
        thread = TicketDmThread.objects.get(ticket=ticket)
        assert (thread.root_ts, thread.generation) == ("", 1)
        tombstoned = False
        assert send("third")
        assert len([thread_ts for _, thread_ts in posts if not thread_ts]) == 2

    def test_refused_root_read_preserves_generation_and_delivers_body(self) -> None:
        ticket = Ticket.objects.create(short_description="Read refusal")
        backend = _backend()
        posts: list[tuple[str, str]] = []

        def post_message(*, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
            posts.append((text, thread_ts))
            return {"ok": True, "ts": f"1700000000.{len(posts):06d}"}

        backend.post_message.side_effect = post_message
        backend.fetch_thread_replies.side_effect = MessagingReadRefusedError(
            "conversations.replies", "1700000000.000001", "ratelimited"
        )
        outcome = notify_user_outcome(
            "body",
            kind=NotifyKind.INFO,
            idempotency_key="read-refusal",
            audience=NotifyAudience.OWNER_QUESTION,
            options=NotifyOptions(backend=backend, user_id="U_ME", ticket_id=ticket.pk),
        )

        assert outcome.sent
        assert len(posts) == 2
        assert posts[0][1] == ""
        assert posts[1][1] == "1700000000.000001"
        thread = TicketDmThread.objects.get(ticket=ticket)
        assert (thread.root_ts, thread.generation) == ("1700000000.000001", 0)

    def test_failed_root_read_delivers_without_a_second_header(self) -> None:
        ticket = Ticket.objects.create(short_description="Read failure")
        backend = _backend()
        posts: list[tuple[str, str]] = []

        def post_message(*, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
            posts.append((text, thread_ts))
            return {"ok": True, "ts": f"1700000000.{len(posts):06d}"}

        backend.post_message.side_effect = post_message
        backend.fetch_thread_replies.side_effect = [OSError("read offline"), [{"ts": "1700000000.000001"}]]

        def send() -> bool:
            return notify_user_outcome(
                "body",
                kind=NotifyKind.INFO,
                idempotency_key="root-read-retry",
                audience=NotifyAudience.OWNER_QUESTION,
                options=NotifyOptions(backend=backend, user_id="U_ME", ticket_id=ticket.pk),
            ).sent

        assert send()
        assert TicketDmThread.objects.get(ticket=ticket).root_ts == "1700000000.000001"
        assert send()
        assert len([thread_ts for _, thread_ts in posts if not thread_ts]) == 1
        assert len(posts) == 2

    def test_header_read_releases_the_write_transaction(self) -> None:
        ticket = Ticket.objects.create(short_description="Read outside lock")
        backend = _backend()
        atomic_depth = len(connection.atomic_blocks)

        def fetch(*, channel: str, thread_ts: str) -> list[RawAPIDict]:
            assert len(connection.atomic_blocks) == atomic_depth
            return [{"ts": thread_ts}]

        backend.fetch_thread_replies.side_effect = fetch
        outcome = notify_user_outcome(
            "body",
            kind=NotifyKind.INFO,
            idempotency_key="read-outside-lock",
            audience=NotifyAudience.OWNER_QUESTION,
            options=NotifyOptions(backend=backend, user_id="U_ME", ticket_id=ticket.pk),
        )

        assert outcome.sent

    def test_schedule_anchor_and_explicit_thread_keep_existing_routing(self) -> None:
        ticket = Ticket.objects.create(issue_url="schedule:daily", short_description="Daily")
        backend = _backend()
        first = notify_user_outcome(
            "schedule note",
            kind=NotifyKind.INFO,
            idempotency_key="schedule-note",
            audience=NotifyAudience.OWNER_QUESTION,
            options=NotifyOptions(backend=backend, user_id="U_ME", ticket_id=ticket.pk),
        )
        second = notify_user_outcome(
            "named thread",
            kind=NotifyKind.INFO,
            idempotency_key="named-thread",
            audience=NotifyAudience.OWNER_QUESTION,
            options=NotifyOptions(backend=backend, user_id="U_ME", ticket_id=ticket.pk, thread_ts="9.0"),
        )

        assert first.sent
        assert second.sent
        assert [call.kwargs["thread_ts"] for call in backend.post_message.call_args_list] == ["", "9.0"]
        assert not TicketDmThread.objects.filter(ticket=ticket).exists()

    def test_ticket_without_issue_or_title_stays_at_dm_root(self) -> None:
        ticket = Ticket.objects.create(short_description="")
        backend = _backend()

        outcome = notify_user_outcome(
            "untitled note",
            kind=NotifyKind.INFO,
            idempotency_key="untitled-ticket",
            audience=NotifyAudience.OWNER_QUESTION,
            options=NotifyOptions(backend=backend, user_id="U_ME", ticket_id=ticket.pk),
        )

        assert outcome.sent
        assert backend.post_message.call_args.kwargs["thread_ts"] == ""
        assert not TicketDmThread.objects.filter(ticket=ticket).exists()

    def test_explicit_send_into_ticket_root_records_its_post(self) -> None:
        ticket = Ticket.objects.create(short_description="Existing thread")
        TicketDmThread.objects.create(ticket=ticket, channel="D-USER", root_ts="1700000000.000001")
        backend = _backend()
        backend.post_message.return_value = {"ok": True, "ts": "1700000000.000002"}

        outcome = notify_user_outcome(
            "progress note",
            kind=NotifyKind.INFO,
            idempotency_key="explicit-ticket-root",
            audience=NotifyAudience.OWNER_QUESTION,
            options=NotifyOptions(backend=backend, user_id="U_ME", thread_ts="1700000000.000001"),
        )

        assert outcome.sent
        assert TicketDmPost.objects.filter(
            channel="D-USER", slack_ts="1700000000.000002", thread__ticket=ticket
        ).exists()

    def test_happy_path_posts_dm_and_records_audit_with_permalink(self) -> None:
        backend = _backend()
        sent = notify_user(
            "tests are green",
            kind=NotifyKind.INFO,
            idempotency_key="sess=a;turn=1",
            audience=NotifyAudience.OWNER_QUESTION,
            backend=backend,
            user_id="U_ME",
        )

        assert sent is True
        backend.open_dm.assert_called_once_with("U_ME")
        backend.post_message.assert_called_once()
        call_kwargs = backend.post_message.call_args.kwargs
        assert call_kwargs["channel"] == "D-USER"
        assert call_kwargs["thread_ts"] == ""
        assert "info" in call_kwargs["text"].lower()
        assert "tests are green" in call_kwargs["text"]

        backend.get_permalink.assert_called_once_with(channel="D-USER", ts="1700000000.000000")

        row = BotPing.objects.get(idempotency_key="sess=a;turn=1")
        assert row.status == BotPing.Status.SENT
        assert row.kind == BotPing.Kind.INFO
        assert row.channel_ref == "D-USER"
        assert row.posted_ts == "1700000000.000000"
        assert row.permalink.startswith("https://")
        assert row.text == "tests are green"

    def test_unnamed_dm_stays_at_root_when_an_inbound_thread_exists(self) -> None:
        IncomingEvent.objects.create(
            source=IncomingEvent.Source.SLACK,
            channel_ref="D-USER",
            thread_ref="1700000000.000111",
            idempotency_key="slack:Ev-active",
        )
        backend = _backend()

        notify_user(
            "answering your question",
            kind=NotifyKind.ANSWER,
            idempotency_key="threaded-answer",
            audience=NotifyAudience.OWNER_QUESTION,
            backend=backend,
            user_id="U_ME",
        )

        assert backend.post_message.call_args.kwargs["thread_ts"] == ""

    def test_unnamed_dm_ignores_the_active_dm_thread(self) -> None:
        IncomingEvent.objects.create(
            source=IncomingEvent.Source.SLACK,
            channel_ref="D-USER",
            thread_ref="1700000000.000111",
            idempotency_key="slack:Ev-active-root-opt-out",
        )
        backend = _backend()

        notify_user_outcome(
            "pending question #7",
            kind=NotifyKind.QUESTION,
            idempotency_key="rooted-question",
            audience=NotifyAudience.OWNER_QUESTION,
            options=NotifyOptions(backend=backend, user_id="U_ME"),
        )

        assert backend.post_message.call_args.kwargs["thread_ts"] == ""

    def test_owner_question_mentions_recipient_in_text_and_section_without_mutating_blocks(self) -> None:
        backend = _backend()
        blocks: list[RawAPIDict] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "Choose a plan"}},
            {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Yes"}}]},
        ]

        notify_user_outcome(
            "Choose a plan",
            kind=NotifyKind.QUESTION,
            idempotency_key="mention-section",
            audience=NotifyAudience.OWNER_QUESTION,
            options=NotifyOptions(backend=backend, user_id="U_ME", blocks=blocks),
        )

        posted = backend.post_message.call_args.kwargs
        assert posted["text"] == "<@U_ME> Choose a plan"
        assert posted["blocks"][0]["text"]["text"] == "<@U_ME> Choose a plan"
        assert posted["blocks"][1] == blocks[1]
        assert blocks[0]["text"]["text"] == "Choose a plan"
        assert BotPing.objects.get(idempotency_key="mention-section").text == "Choose a plan"

    def test_owner_question_prepends_section_when_first_block_is_not_mrkdwn(self) -> None:
        backend = _backend()
        blocks: list[RawAPIDict] = [{"type": "actions", "elements": []}]

        notify_user_outcome(
            "Choose a plan",
            kind=NotifyKind.QUESTION,
            idempotency_key="mention-new-section",
            audience=NotifyAudience.OWNER_QUESTION,
            options=NotifyOptions(backend=backend, user_id="U_ME", blocks=blocks),
        )

        assert backend.post_message.call_args.kwargs["blocks"] == [
            {"type": "section", "text": {"type": "mrkdwn", "text": "<@U_ME>"}},
            *blocks,
        ]
        assert blocks == [{"type": "actions", "elements": []}]

    def test_owner_question_with_empty_mrkdwn_block_still_posts(self) -> None:
        backend = _backend()
        blocks: list[RawAPIDict] = [{"type": "section", "text": {"type": "mrkdwn"}}]

        outcome = notify_user_outcome(
            "Choose a plan",
            kind=NotifyKind.QUESTION,
            idempotency_key="mention-empty-mrkdwn",
            audience=NotifyAudience.OWNER_QUESTION,
            options=NotifyOptions(backend=backend, user_id="U_ME", blocks=blocks),
        )

        assert outcome.sent
        assert backend.post_message.call_args.kwargs["blocks"] == [
            {"type": "section", "text": {"type": "mrkdwn", "text": "<@U_ME>"}},
            *blocks,
        ]

    def test_owner_question_existing_mention_and_other_audience_stay_unchanged(self) -> None:
        backend = _backend()
        blocks: list[RawAPIDict] = [{"type": "section", "text": {"type": "mrkdwn", "text": "<@U_ME> choose"}}]

        notify_user_outcome(
            "<@U_ME> choose",
            kind=NotifyKind.QUESTION,
            idempotency_key="mention-already-present",
            audience=NotifyAudience.OWNER_QUESTION,
            options=NotifyOptions(backend=backend, user_id="U_ME", blocks=blocks),
        )
        assert backend.post_message.call_args.kwargs["text"] == "<@U_ME> choose"
        assert backend.post_message.call_args.kwargs["blocks"] == blocks

        notify_user_outcome(
            "Choose a plan",
            kind=NotifyKind.QUESTION,
            idempotency_key="mention-in-blocks",
            audience=NotifyAudience.OWNER_QUESTION,
            options=NotifyOptions(backend=backend, user_id="U_ME", blocks=blocks),
        )
        assert backend.post_message.call_args.kwargs["text"] == "<@U_ME> Choose a plan"
        assert backend.post_message.call_args.kwargs["blocks"] == blocks

        notify_user_outcome(
            "Choose a plan",
            kind=NotifyKind.QUESTION,
            idempotency_key="other-audience-unchanged",
            audience=NotifyAudience.COLLEAGUE_ACTION,
            options=NotifyOptions(backend=backend, user_id="U_ME", blocks=blocks, requested_push=True),
        )
        assert backend.post_message.call_args.kwargs["text"] == "Choose a plan"
        assert backend.post_message.call_args.kwargs["blocks"] == blocks

    def test_no_active_thread_posts_at_root(self) -> None:
        backend = _backend()

        notify_user(
            "first message in the conversation",
            kind=NotifyKind.INFO,
            idempotency_key="rootless",
            audience=NotifyAudience.OWNER_QUESTION,
            backend=backend,
            user_id="U_ME",
        )

        assert backend.post_message.call_args.kwargs["thread_ts"] == ""

    def test_table_blocks_reach_post_message_with_fence_fallback(self) -> None:
        # A caller with tabular data passes the monospace fence as ``text`` and
        # the native ``table`` block as ``blocks``; both must reach post_message.
        from teatree.backends.slack.table_format import render_table_message  # noqa: PLC0415

        backend = _backend()
        message = render_table_message(["ID", "Name"], [["1", "Alice"]], title="Records")

        notify_user(
            message.fence,
            kind=NotifyKind.INFO,
            idempotency_key="table-dm",
            audience=NotifyAudience.OWNER_QUESTION,
            backend=backend,
            user_id="U_ME",
            blocks=message.blocks,
            linkify=False,
        )

        call_kwargs = backend.post_message.call_args.kwargs
        assert [b["type"] for b in call_kwargs["blocks"]] == ["section", "table"]
        assert "```" in call_kwargs["text"]

    def test_no_blocks_kwarg_on_the_text_only_path(self) -> None:
        # A plain notification calls post_message without a ``blocks`` kwarg, so
        # a backend predating the argument keeps working unchanged.
        backend = _backend()

        notify_user(
            "plain text notification",
            kind=NotifyKind.INFO,
            idempotency_key="plain-no-blocks",
            audience=NotifyAudience.OWNER_QUESTION,
            backend=backend,
            user_id="U_ME",
        )

        assert "blocks" not in backend.post_message.call_args.kwargs

    def test_kind_accepts_string_alias(self) -> None:
        backend = _backend()
        sent = notify_user(
            "draft reply ready",
            kind="answer",
            idempotency_key="alias-str",
            audience=NotifyAudience.OWNER_QUESTION,
            backend=backend,
            user_id="U_ME",
        )

        assert sent is True
        assert BotPing.objects.get(idempotency_key="alias-str").kind == BotPing.Kind.ANSWER

    def test_duplicate_idempotency_key_is_noop(self) -> None:
        backend = _backend()
        notify_user(
            "first",
            kind=NotifyKind.INFO,
            idempotency_key="dup",
            audience=NotifyAudience.OWNER_QUESTION,
            backend=backend,
            user_id="U_ME",
        )
        sent2 = notify_user(
            "second",
            kind=NotifyKind.INFO,
            idempotency_key="dup",
            audience=NotifyAudience.OWNER_QUESTION,
            backend=backend,
            user_id="U_ME",
        )

        assert sent2 is True  # the prior send was SENT
        assert backend.post_message.call_count == 1
        assert BotPing.objects.filter(idempotency_key="dup").count() == 1

    def test_reentrant_concurrent_tick_during_delivery_does_not_double_dm(self) -> None:
        """A second tick firing *during* the first tick's delivery must NOT also deliver.

        The pre-fix shape delivered BEFORE recording the SENT row, so a second
        tick re-reading in that window saw no SENT row, deleted any recoverable
        row, and delivered again — a double-DM. The fix claims a SENDING row
        atomically *before* delivering, so the re-entrant second tick's
        ``claim_delivery`` observes the in-flight claim and stands down.

        This drives the race deterministically: the first tick's ``open_dm``
        re-enters ``notify_user`` with the same key (the concurrent second
        tick) before the first tick has finalized. Exactly one
        ``post_message`` lands and exactly one SENT row survives.
        """
        backend = _backend()
        second_outcome: dict[str, bool] = {}

        def _reentrant_open_dm(user_id: str) -> str:
            if "second" not in second_outcome:
                second_outcome["second"] = notify_user(
                    "concurrent second tick",
                    kind=NotifyKind.INFO,
                    idempotency_key="toctou",
                    audience=NotifyAudience.OWNER_QUESTION,
                    backend=backend,
                    user_id="U_ME",
                )
            return "D-USER"

        backend.open_dm.side_effect = _reentrant_open_dm

        first = notify_user(
            "first tick",
            kind=NotifyKind.INFO,
            idempotency_key="toctou",
            audience=NotifyAudience.OWNER_QUESTION,
            backend=backend,
            user_id="U_ME",
        )

        assert first is True
        assert second_outcome["second"] is False  # the re-entrant tick stood down
        assert backend.post_message.call_count == 1, "double-DM: delivery fired twice"
        assert BotPing.objects.filter(idempotency_key="toctou", status=BotPing.Status.SENT).count() == 1
        assert BotPing.objects.filter(idempotency_key="toctou").count() == 1

    def test_prior_failed_attempt_does_not_block_retry(self) -> None:
        """Regression for #1306: a FAILED BotPing must not block a sub-agent retry.

        Pre-fix the idempotency-key ledger was strict: ANY existing row
        (FAILED, NOOP, SENT) made the helper return the prior outcome
        without retrying. A sub-agent that hit a transient Slack failure
        was permanently locked out — the only escape was a fresh key.

        verify-by-re-read + idempotency means: SENT stays a no-op,
        FAILED/NOOP are recoverable. The prior failed row is replaced
        when delivery succeeds, so the audit trail still reflects the
        terminal outcome.
        """
        # First attempt — transport raises, ledger records FAILED.
        bad_backend = _backend()
        bad_backend.post_message.side_effect = RuntimeError("transient slack 500")
        assert (
            notify_user(
                "retry me",
                kind=NotifyKind.INFO,
                idempotency_key="retry-1306",
                audience=NotifyAudience.OWNER_QUESTION,
                backend=bad_backend,
                user_id="U_ME",
            )
            is False
        )
        assert BotPing.objects.get(idempotency_key="retry-1306").status == BotPing.Status.FAILED

        # Second attempt with a working backend — must actually deliver, not no-op.
        good_backend = _backend()
        assert (
            notify_user(
                "retry me",
                kind=NotifyKind.INFO,
                idempotency_key="retry-1306",
                audience=NotifyAudience.OWNER_QUESTION,
                backend=good_backend,
                user_id="U_ME",
            )
            is True
        )
        good_backend.open_dm.assert_called_once_with("U_ME")
        good_backend.post_message.assert_called_once()
        assert bad_backend.post_message.call_args.kwargs["text"].count("<@U_ME>") == 1
        assert good_backend.post_message.call_args.kwargs["text"].count("<@U_ME>") == 1
        assert BotPing.objects.get(idempotency_key="retry-1306").text == "retry me"
        # The terminal ledger row reflects the eventual success.
        assert BotPing.objects.get(idempotency_key="retry-1306").status == BotPing.Status.SENT

    def test_missing_backend_records_noop_and_returns_false(self) -> None:
        with patch("teatree.core.notify.messaging_from_overlay", return_value=None):
            sent = notify_user(
                "no backend",
                kind=NotifyKind.QUESTION,
                idempotency_key="noop-backend",
                audience=NotifyAudience.OWNER_QUESTION,
                backend=None,
                user_id="U_ME",
            )

        assert sent is False
        row = BotPing.objects.get(idempotency_key="noop-backend")
        assert row.status == BotPing.Status.NOOP
        assert row.permalink == ""

    def test_missing_user_id_records_noop_and_returns_false(self) -> None:
        backend = _backend()
        sent = notify_user(
            "no user id",
            kind=NotifyKind.QUESTION,
            idempotency_key="noop-uid",
            audience=NotifyAudience.OWNER_QUESTION,
            backend=backend,
            user_id="",
        )

        assert sent is False
        backend.open_dm.assert_not_called()
        row = BotPing.objects.get(idempotency_key="noop-uid")
        assert row.status == BotPing.Status.NOOP

    def test_transport_exception_is_swallowed_and_audited(self) -> None:
        backend = _backend()
        backend.post_message.side_effect = RuntimeError("slack timeout")
        sent = notify_user(
            "boom",
            kind=NotifyKind.INFO,
            idempotency_key="failed",
            audience=NotifyAudience.OWNER_QUESTION,
            backend=backend,
            user_id="U_ME",
        )

        assert sent is False
        row = BotPing.objects.get(idempotency_key="failed")
        assert row.status == BotPing.Status.FAILED
        assert "slack timeout" in row.error_message

    def test_empty_channel_from_open_dm_is_hard_failure(self) -> None:
        """``open_dm`` returns ``""`` on Slack ``ok:false`` (e.g. missing_scope).

        The bug this guards: the empty channel was treated as benign, the
        DM never landed, yet ``BotPing`` was marked ``SENT`` and the
        function returned ``True`` — a silent success. The contract is a
        HARD FAILURE: ``FAILED`` row, ``False`` return, no claim recorded.
        """
        backend = _backend()
        backend.open_dm.return_value = ""

        sent = notify_user(
            "this never lands",
            kind=NotifyKind.INFO,
            idempotency_key="empty-channel",
            audience=NotifyAudience.OWNER_QUESTION,
            backend=backend,
            user_id="U_ME",
        )

        assert sent is False
        backend.post_message.assert_not_called()
        row = BotPing.objects.get(idempotency_key="empty-channel")
        assert row.status == BotPing.Status.FAILED
        assert "open_dm" in row.error_message or "channel" in row.error_message
        from teatree.core.models import OutboundClaim  # noqa: PLC0415

        assert not OutboundClaim.objects.filter(idempotency_key="slack_dm:empty-channel").exists()

    def test_post_message_ok_false_is_hard_failure(self) -> None:
        """``post_message`` returning ``{"ok": false}`` must be a HARD FAILURE.

        Slack returns ``ok:false`` (missing_scope, channel_not_found, …)
        with no ``ts``. The pre-fix code recorded ``SENT`` + returned
        ``True`` because it only looked at ``response.get("ts")``.
        """
        backend = _backend()
        backend.post_message.return_value = {"ok": False, "error": "missing_scope"}

        sent = notify_user(
            "not actually posted",
            kind=NotifyKind.INFO,
            idempotency_key="ok-false",
            audience=NotifyAudience.OWNER_QUESTION,
            backend=backend,
            user_id="U_ME",
        )

        assert sent is False
        row = BotPing.objects.get(idempotency_key="ok-false")
        assert row.status == BotPing.Status.FAILED
        assert "missing_scope" in row.error_message
        from teatree.core.models import OutboundClaim  # noqa: PLC0415

        assert not OutboundClaim.objects.filter(idempotency_key="slack_dm:ok-false").exists()

    def test_post_message_empty_ts_is_hard_failure(self) -> None:
        """An ``ok:true`` response with no ``ts`` still means nothing landed."""
        backend = _backend()
        backend.post_message.return_value = {"ok": True}

        sent = notify_user(
            "phantom success",
            kind=NotifyKind.INFO,
            idempotency_key="empty-ts",
            audience=NotifyAudience.OWNER_QUESTION,
            backend=backend,
            user_id="U_ME",
        )

        assert sent is False
        row = BotPing.objects.get(idempotency_key="empty-ts")
        assert row.status == BotPing.Status.FAILED
        from teatree.core.models import OutboundClaim  # noqa: PLC0415

        assert not OutboundClaim.objects.filter(idempotency_key="slack_dm:empty-ts").exists()

    def test_permalink_lookup_failure_does_not_break_send(self) -> None:
        backend = _backend()
        backend.get_permalink.side_effect = RuntimeError("permalink api down")
        sent = notify_user(
            "still sent",
            kind=NotifyKind.INFO,
            idempotency_key="permalink-fail",
            audience=NotifyAudience.OWNER_QUESTION,
            backend=backend,
            user_id="U_ME",
        )

        assert sent is True
        row = BotPing.objects.get(idempotency_key="permalink-fail")
        assert row.status == BotPing.Status.SENT
        assert row.permalink == ""


class TestNotifyUserUnderAnEgressSuppressor(TestCase):
    def test_owner_dm_reaches_the_suppressor_and_never_the_backend(self) -> None:
        backend = _backend()
        suppressed: list[tuple[str, str, EgressKind]] = []

        def record(target: str, action: str, kind: EgressKind) -> RawAPIDict:
            suppressed.append((target, action, kind))
            return {"ok": True, "ts": "dry-run"}

        with suppress_on_behalf_egress(record):
            notify_user(
                "posted on your behalf",
                kind=NotifyKind.INFO,
                idempotency_key="preview-owner-dm",
                audience=NotifyAudience.OWNER_QUESTION,
                backend=backend,
                user_id="U_ME",
            )

        assert suppressed == [("U_ME", "owner_dm_post", EgressKind.POST)]
        backend.open_dm.assert_not_called()
        backend.post_message.assert_not_called()


class TestNotifyUserNeverRaises(TestCase):
    """The never-raise contract holds for any DatabaseError, not just IntegrityError.

    ``notify_user`` runs inside FSM transitions and the public docstring
    promises it never raises into the CLI turn. Pre-fix the SENT-audit
    write caught only ``IntegrityError``, so an ``OperationalError`` (e.g.
    SQLite "database is locked") raised by the ``BotPing`` write — after
    the DM had already landed — escaped and broke the caller's transition.
    The same too-narrow catch applied to the NOOP/FAILED audit writes and
    to the early delivery-claim. The most-defensive sibling
    (``_record_outbound_claim``) already swallows the full ``DatabaseError``
    breadth; every DB access in ``notify_user`` must match it so no
    ``DatabaseError`` reaches the caller.
    """

    def test_operational_error_on_delivery_claim_is_swallowed(self) -> None:
        backend = _backend()
        with patch.object(BotPing, "claim_delivery", side_effect=_DB_LOCKED):
            sent = notify_user(
                "delivery claim under lock contention",
                kind=NotifyKind.INFO,
                idempotency_key="db-locked-claim",
                audience=NotifyAudience.OWNER_QUESTION,
                backend=backend,
                user_id="U_ME",
            )

        # Fail closed: no delivery, no propagation, caller keeps moving.
        assert sent is False
        backend.post_message.assert_not_called()

    def test_operational_error_on_sent_finalize_is_swallowed(self) -> None:
        backend = _backend()
        with patch.object(BotPing, "finalize_sent", side_effect=_DB_LOCKED):
            sent = notify_user(
                "lock contention on the sent finalize",
                kind=NotifyKind.INFO,
                idempotency_key="db-locked-sent",
                audience=NotifyAudience.OWNER_QUESTION,
                backend=backend,
                user_id="U_ME",
            )

        # DM landed; the failed finalize write must not propagate or flip the result.
        assert sent is True
        backend.post_message.assert_called_once()

    def test_operational_error_on_noop_audit_is_swallowed(self) -> None:
        with (
            patch("teatree.core.notify.messaging_from_overlay", return_value=None),
            patch.object(BotPing.objects, "create", side_effect=_DB_LOCKED),
        ):
            sent = notify_user(
                "no backend, locked audit",
                kind=NotifyKind.QUESTION,
                idempotency_key="db-locked-noop",
                audience=NotifyAudience.OWNER_QUESTION,
                backend=None,
                user_id="U_ME",
            )

        assert sent is False

    def test_operational_error_on_failed_finalize_is_swallowed(self) -> None:
        backend = _backend()
        backend.post_message.side_effect = RuntimeError("slack timeout")
        with patch.object(BotPing, "finalize_failed", side_effect=_DB_LOCKED):
            sent = notify_user(
                "delivery failed, locked finalize",
                kind=NotifyKind.INFO,
                idempotency_key="db-locked-failed",
                audience=NotifyAudience.OWNER_QUESTION,
                backend=backend,
                user_id="U_ME",
            )

        assert sent is False


class TestNotifyUserLinkify(TestCase):
    """Slack mrkdwn rewrite is applied to the payload, not the audit row."""

    def test_dashboard_md_links_become_mrkdwn(self) -> None:
        backend = _backend()
        text = "see [the PR](https://example.com/pr/1) for details"
        sent = notify_user(
            text,
            kind=NotifyKind.INFO,
            idempotency_key="linkify-md",
            audience=NotifyAudience.OWNER_QUESTION,
            backend=backend,
            user_id="U_ME",
        )

        assert sent is True
        sent_text = backend.post_message.call_args.kwargs["text"]
        assert "<https://example.com/pr/1|the PR>" in sent_text
        # The original markdown form is GONE from the payload
        assert "[the PR](https://example.com/pr/1)" not in sent_text
        # The audit row keeps the original
        row = BotPing.objects.get(idempotency_key="linkify-md")
        assert row.text == text

    def test_bare_mr_token_uses_overlay_resolver(self) -> None:
        backend = _backend()
        fake_overlay = MagicMock()
        fake_overlay.resolve_mr_token.side_effect = lambda n: (
            f"https://gitlab.example.com/group/repo-a/-/merge_requests/{n}" if n == 281 else None
        )
        fake_overlay.resolve_issue_token.return_value = None

        with patch("teatree.core.overlay_loader.get_overlay", return_value=fake_overlay):
            notify_user(
                "approve !281 then !999",
                kind=NotifyKind.INFO,
                idempotency_key="linkify-mr",
                audience=NotifyAudience.OWNER_QUESTION,
                backend=backend,
                user_id="U_ME",
            )

        sent_text = backend.post_message.call_args.kwargs["text"]
        assert "<https://gitlab.example.com/group/repo-a/-/merge_requests/281|!281>" in sent_text
        # Unresolved token stays bare — better inert than wrong
        assert "!999" in sent_text
        assert "<https://gitlab.example.com/group/repo-a/-/merge_requests/999" not in sent_text

    def test_linkify_false_opts_out(self) -> None:
        backend = _backend()
        text = "raw [link](https://example.com) stays raw"
        notify_user(
            text,
            kind=NotifyKind.INFO,
            idempotency_key="linkify-off",
            audience=NotifyAudience.OWNER_QUESTION,
            backend=backend,
            user_id="U_ME",
            linkify=False,
        )

        sent_text = backend.post_message.call_args.kwargs["text"]
        assert "[link](https://example.com)" in sent_text
        assert "<https://example.com|link>" not in sent_text

    def test_overlay_resolution_failure_does_not_break_send(self) -> None:
        backend = _backend()
        with patch("teatree.core.overlay_loader.get_overlay", side_effect=RuntimeError("no overlay")):
            sent = notify_user(
                "ship [the PR](https://example.com/pr/1) and !281",
                kind=NotifyKind.INFO,
                idempotency_key="linkify-overlay-fail",
                audience=NotifyAudience.OWNER_QUESTION,
                backend=backend,
                user_id="U_ME",
            )

        assert sent is True
        sent_text = backend.post_message.call_args.kwargs["text"]
        # Markdown link rewrite still works (no overlay needed)
        assert "<https://example.com/pr/1|the PR>" in sent_text
        # Bare MR token stayed bare because no resolver
        assert "!281" in sent_text

    def test_workaround_user_id_kwarg_still_supported(self) -> None:
        """``notify_user(user_id="U0DEMOUSER1", audience=NotifyAudience.OWNER_QUESTION)`` workaround must still work."""
        backend = _backend()
        sent = notify_user(
            "ping",
            kind=NotifyKind.INFO,
            idempotency_key="user-id-workaround",
            audience=NotifyAudience.OWNER_QUESTION,
            backend=backend,
            user_id="U0DEMOUSER1",
        )

        assert sent is True
        backend.open_dm.assert_called_once_with("U0DEMOUSER1")


class TestPublicHelperSurface:
    """The three cross-package helpers are public names, not underscored privates.

    ``teatree.messaging.notify_with_fallback`` and the live-approval CLI
    reach for these helpers across the package boundary, so they are part
    of the module's public surface and must not carry a leading underscore.
    """

    def test_public_helpers_are_importable(self) -> None:
        assert callable(notify_module.format_notification)
        assert callable(notify_module.maybe_linkify)
        assert callable(notify_module.resolve_user_id)

    def test_old_private_names_are_gone(self) -> None:
        assert not hasattr(notify_module, "_format")
        assert not hasattr(notify_module, "_maybe_linkify")
        assert not hasattr(notify_module, "_resolve_user_id")

    def test_format_notification_prefixes_kind_marker(self) -> None:
        out = notify_module.format_notification("hello", NotifyKind.INFO)
        assert "hello" in out
        assert "info" in out.lower()

    def test_a_question_is_sent_exactly_as_its_card_was_rendered(self) -> None:
        card = "Can I ship it? Yes, now. 1. One thing.\nA sentence. And a second one.\n\n[Yes] - I go."

        assert notify_module.format_notification(card, NotifyKind.QUESTION) == card


class TestResolveUserId(TestCase):
    """``resolve_user_id``: overlay registry → global DB ``slack_user_id`` → empty."""

    def test_global_db_fallback_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite3"
            _seed_config(db, "slack_user_id", "U-GLOBAL")
            with patch.dict(os.environ, {"T3_CONFIG_DB": str(db)}):
                os.environ.pop("T3_OVERLAY_NAME", None)
                assert notify_module.resolve_user_id() == "U-GLOBAL"

    def test_overlay_registry_beats_global_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite3"
            _seed_config(db, "slack_user_id", "U-GLOBAL")
            _seed_config(db, "overlays", {"acme": {"slack_user_id": "U-OVERLAY"}})
            with patch.dict(os.environ, {"T3_CONFIG_DB": str(db), "T3_OVERLAY_NAME": "acme"}):
                assert notify_module.resolve_user_id() == "U-OVERLAY"

    def test_empty_when_nothing_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite3"
            _seed_config(db, "unrelated", "x")  # a DB with the table but no slack_user_id row
            with patch.dict(os.environ, {"T3_CONFIG_DB": str(db)}):
                os.environ.pop("T3_OVERLAY_NAME", None)
                assert notify_module.resolve_user_id() == ""


class ResolveOwnerDmBackendFailClosedTests(TestCase):
    def test_a_broken_transport_resolution_is_treated_as_no_transport(self) -> None:
        with patch("teatree.core.notify.messaging_from_overlay", side_effect=RuntimeError("bad entry point")):
            backend, reason = resolve_owner_dm_backend()
        assert backend is None
        assert reason is NotifyReason.NO_MESSAGING_BACKEND
