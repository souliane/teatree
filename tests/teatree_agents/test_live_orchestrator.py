import asyncio
import json
import sqlite3
import tempfile
import threading
import time
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.db import DatabaseError, connections
from django.test import TransactionTestCase
from django.utils import timezone

from teatree.agents import live_orchestrator
from teatree.agents.live_delivery import InboxPage
from teatree.agents.live_mailbox import LiveMailboxBroker
from teatree.agents.live_mailbox_client import MailboxClient, MailboxError
from teatree.agents.live_orchestrator import start_orchestrator_peer
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.models import Session
from teatree.core.models.deferred_question import DeferredQuestion
from tests._owner_channel import owner_card
from tests.factories import planned_ticket


class TestLiveOrchestrator(TransactionTestCase):
    def test_broker_close_stops_orchestrator_before_socket_shutdown(self) -> None:
        with (
            tempfile.TemporaryDirectory(prefix="t3-o-", dir="/tmp") as directory,
            LiveMailboxBroker(runtime_dir=Path(directory)) as broker,
        ):
            peer = start_orchestrator_peer(broker)
            assert peer.is_alive()

        assert not peer.is_alive()

    def setUp(self) -> None:
        super().setUp()
        self.db_directory = tempfile.TemporaryDirectory(prefix="t3-orchestrator-db-")
        self.db_path = Path(self.db_directory.name) / "shared.sqlite3"
        connection = connections["default"]
        connection.ensure_connection()
        self.original_db_name = connection.settings_dict["NAME"]
        with closing(sqlite3.connect(self.db_path)) as target:
            connection.connection.backup(target)
        connection.settings_dict["NAME"] = str(self.db_path)
        connection.close()

    def tearDown(self) -> None:
        connection = connections["default"]
        connection.close()
        connection.settings_dict["NAME"] = self.original_db_name
        connection.ensure_connection()
        with closing(sqlite3.connect(self.db_path)) as source:
            source.backup(connection.connection)
        self.db_directory.cleanup()
        super().tearDown()

    def _ask(self, client: MailboxClient, address: str, nonce: str, kind: str) -> dict[str, object]:
        payload = json.dumps({"type": "ask", "nonce": nonce, "kind": kind})
        asyncio.run(client.send(address, payload, f"ask:{nonce}", timeout_seconds=0.1))
        cursor = 0
        for _ in range(10):
            page = asyncio.run(client.wait(after_id=cursor, timeout_seconds=0.5))
            cursor = page["next_cursor"]
            for message in page["messages"]:
                answer = json.loads(message["text"])
                if answer["answer_to"] == nonce:
                    return answer
        message = "orchestrator did not reply"
        raise AssertionError(message)

    def test_design_replies_are_ticket_bound_and_nonce_bound(self) -> None:
        ticket = planned_ticket()
        session = Session.objects.create(ticket=ticket)
        DeferredQuestion.objects.create(
            question="Which scope?",
            audience=DeferredQuestion.Audience.OWNER_QUESTION,
            evidence=owner_card(OwnerDecision.ARCHITECTURE).to_evidence(),
            answered_at=timezone.now(),
            answer_text="The smaller scope.",
            resolved_via=DeferredQuestion.ResolvedVia.SLACK,
            task_session=session,
        )
        other = planned_ticket()
        with (
            tempfile.TemporaryDirectory(prefix="t3-o-", dir="/tmp") as directory,
            LiveMailboxBroker(runtime_dir=Path(directory)) as broker,
        ):
            peer = start_orchestrator_peer(broker)
            asker = broker.register(room=str(ticket.pk), harness="claude_sdk", label="asker")
            stranger = broker.register(room=str(other.pk), harness="codex_app_server", label="stranger")
            address = next(p["address"] for p in broker.identities() if p["harness"] == "orchestrator")
            assert len([p for p in broker.identities() if p["harness"] == "orchestrator"]) == 1
            start_orchestrator_peer(broker)
            assert len([p for p in broker.identities() if p["harness"] == "orchestrator"]) == 1
            first = self._ask(MailboxClient(asker.socket_path, asker.token), address, "nonce-one", "architecture")
            second = self._ask(MailboxClient(asker.socket_path, asker.token), address, "nonce-two", "architecture")
            foreign = self._ask(
                MailboxClient(stranger.socket_path, stranger.token), address, "nonce-three", "architecture"
            )
            assert first["answer_to"] == "nonce-one"
            assert second["answer_to"] == "nonce-two"
            assert first["verdict"] == second["verdict"] == "recorded"
            assert first["decisions"] == [{"question": "Which scope?", "answer": "The smaller scope."}]
            assert foreign["verdict"] == "not_settled"
            assert foreign["decisions"] == []
            broker.unregister(address)
            peer.join(timeout=3)
        assert not peer.is_alive()

    def test_transient_broker_error_keeps_the_peer_available(self) -> None:
        ticket = planned_ticket()
        original_wait = MailboxClient.wait
        failed = threading.Event()

        async def fail_once_for_orchestrator(
            client: MailboxClient, *, after_id: int = 0, timeout_seconds: float = 20
        ) -> InboxPage:
            if threading.current_thread().name.startswith("t3-orchestrator") and not failed.is_set():
                failed.set()
                message = "transient"
                raise MailboxError(message)
            return await original_wait(client, after_id=after_id, timeout_seconds=timeout_seconds)

        with (
            tempfile.TemporaryDirectory(prefix="t3-o-", dir="/tmp") as directory,
            LiveMailboxBroker(runtime_dir=Path(directory)) as broker,
            patch.object(MailboxClient, "wait", fail_once_for_orchestrator),
        ):
            peer = start_orchestrator_peer(broker)
            assert failed.wait(timeout=2)
            asker = broker.register(room=str(ticket.pk), harness="claude_sdk", label="asker")
            address = next(p["address"] for p in broker.identities() if p["harness"] == "orchestrator")
            answer = self._ask(MailboxClient(asker.socket_path, asker.token), address, "after-error", "architecture")
            assert answer["answer_to"] == "after-error"
            broker.unregister(address)
            peer.join(timeout=3)
            assert not peer.is_alive()

    def test_hard_limit_and_malformed_ask_do_not_create_cards(self) -> None:
        ticket = planned_ticket()
        DeferredQuestion.objects.create(
            question="Which old scope?",
            audience=DeferredQuestion.Audience.OWNER_QUESTION,
            evidence=owner_card(OwnerDecision.PRODUCT_SCOPE).to_evidence(),
            answered_at=timezone.now(),
            answer_text="The old scope.",
            resolved_via=DeferredQuestion.ResolvedVia.SLACK,
            task_session=Session.objects.create(ticket=ticket),
        )
        before = DeferredQuestion.objects.count()
        with (
            tempfile.TemporaryDirectory(prefix="t3-o-", dir="/tmp") as directory,
            LiveMailboxBroker(runtime_dir=Path(directory)) as broker,
        ):
            peer = start_orchestrator_peer(broker)
            asker = broker.register(room=str(ticket.pk), harness="claude_sdk", label="asker")
            client = MailboxClient(asker.socket_path, asker.token)
            address = next(p["address"] for p in broker.identities() if p["harness"] == "orchestrator")
            asyncio.run(client.send(address, "not json", "invalid", timeout_seconds=0.1))
            malformed = asyncio.run(client.wait(timeout_seconds=2))
            assert json.loads(malformed["messages"][0]["text"])["verdict"] == "malformed"
            reply = self._ask(client, address, "nonce-valid", "credentials")
            assert reply["answer_to"] == "nonce-valid"
            assert reply["verdict"] == "owner_only"
            assert reply["decisions"] == []
            legacy = self._ask(client, address, "nonce-legacy", "product_scope")
            assert legacy["verdict"] == "unsupported"
            assert legacy["decisions"] == []
            with patch.object(live_orchestrator, "_recorded_decisions", side_effect=DatabaseError("offline")):
                unavailable = self._ask(client, address, "nonce-unavailable", "architecture")
            assert unavailable["verdict"] == "unavailable"
            assert unavailable["decisions"] == []
            broker.unregister(address)
            peer.join(timeout=3)
        assert DeferredQuestion.objects.count() == before

    def test_only_three_newest_decisions_with_bounded_text_are_returned(self) -> None:
        ticket = planned_ticket()
        session = Session.objects.create(ticket=ticket)
        for index in range(4):
            DeferredQuestion.objects.create(
                question=f"{index}:" + "q" * 500,
                audience=DeferredQuestion.Audience.OWNER_QUESTION,
                evidence=owner_card(OwnerDecision.ARCHITECTURE).to_evidence(),
                answered_at=timezone.now() + timedelta(seconds=index),
                answer_text="é" * 500,
                resolved_via=DeferredQuestion.ResolvedVia.SLACK,
                task_session=session,
            )
        with (
            tempfile.TemporaryDirectory(prefix="t3-o-", dir="/tmp") as directory,
            LiveMailboxBroker(runtime_dir=Path(directory)) as broker,
        ):
            peer = start_orchestrator_peer(broker)
            asker = broker.register(room=str(ticket.pk), harness="claude_sdk", label="asker")
            address = next(p["address"] for p in broker.identities() if p["harness"] == "orchestrator")
            answer = self._ask(MailboxClient(asker.socket_path, asker.token), address, "cap", "architecture")
            assert answer["verdict"] == "recorded"
            decisions = answer["decisions"]
            assert isinstance(decisions, list)
            assert len(decisions) == 3
            assert [row["question"][:2] for row in decisions] == ["3:", "2:", "1:"]
            assert all(len(row["question"]) == len(row["answer"]) == 400 for row in decisions)
            assert len(json.dumps(answer).encode()) < 16_384
            broker.unregister(address)
            peer.join(timeout=3)

    def test_peer_rotates_before_its_send_key_cap(self) -> None:
        ticket = planned_ticket()
        with (
            tempfile.TemporaryDirectory(prefix="t3-o-", dir="/tmp") as directory,
            LiveMailboxBroker(runtime_dir=Path(directory)) as broker,
            patch.object(live_orchestrator, "MAX_SENT_KEYS", 4),
        ):
            peer = start_orchestrator_peer(broker)
            asker = broker.register(room=str(ticket.pk), harness="claude_sdk", label="asker")
            client = MailboxClient(asker.socket_path, asker.token)
            first = next(p["address"] for p in broker.identities() if p["harness"] == "orchestrator")
            self._ask(client, first, "one", "architecture")
            self._ask(client, first, "two", "architecture")
            for _ in range(50):
                second = next(
                    (
                        p["address"]
                        for p in broker.identities()
                        if p["harness"] == "orchestrator" and p["address"] != first
                    ),
                    first,
                )
                if second != first:
                    break
                time.sleep(0.01)
            assert first != second
            assert self._ask(client, second, "three", "architecture")["answer_to"] == "three"
            broker.unregister(second)
            peer.join(timeout=3)
