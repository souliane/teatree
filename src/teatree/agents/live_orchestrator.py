"""Answer settled agent questions on the worker's direct mailbox."""

import asyncio
import json
import logging
import threading
from contextlib import suppress
from typing import NotRequired, TypedDict

from django.db import DatabaseError, close_old_connections

from teatree.agents import live_mailbox
from teatree.agents.live_delivery import MAX_SENT_KEYS, MailboxIdentity
from teatree.agents.live_mailbox_client import MailboxClient, MailboxError
from teatree.core.modelkit.owner_decision import OWNER_KEPT_DECISIONS, OWNER_RAISABLE_DECISIONS, OwnerDecision
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.utils.thread_db import close_thread_db_connections

logger = logging.getLogger(__name__)
MAX_DECISIONS = 3
MAX_FIELD_CHARS = 400
MAX_NONCE_CHARS = 64


class OrchestratorAnswer(TypedDict):
    type: str
    answer_to: str | None
    verdict: str
    decisions: list[dict[str, str]]
    limit: NotRequired[str]


def _recorded_decisions(ticket: str, kind: OwnerDecision) -> list[dict[str, str]]:
    if not ticket.isdecimal():
        return []
    close_old_connections()
    rows = DeferredQuestion.objects.owner_answers_on_ticket(int(ticket), kind)[:MAX_DECISIONS]
    return [{"question": row.question[:MAX_FIELD_CHARS], "answer": row.answer_text[:MAX_FIELD_CHARS]} for row in rows]


def _answer(text: str, ticket: str) -> OrchestratorAnswer:
    nonce: str | None = None
    try:
        request = json.loads(text)
    except (ValueError, TypeError, RecursionError):
        request = None
    kind: OwnerDecision | None = None
    if isinstance(request, dict):
        raw_nonce = request.get("nonce")
        if isinstance(raw_nonce, str) and 0 < len(raw_nonce) <= MAX_NONCE_CHARS:
            nonce = raw_nonce
        with suppress(ValueError, TypeError):
            kind = OwnerDecision(request.get("kind"))
    if not isinstance(request, dict) or request.get("type") != "ask" or nonce is None or kind is None:
        return {"type": "answer", "answer_to": nonce, "verdict": "malformed", "decisions": []}
    if kind not in OWNER_RAISABLE_DECISIONS:
        return {"type": "answer", "answer_to": nonce, "verdict": "unsupported", "decisions": []}
    if kind in OWNER_KEPT_DECISIONS:
        return {"type": "answer", "answer_to": nonce, "verdict": "owner_only", "decisions": [], "limit": kind.value}
    try:
        decisions = _recorded_decisions(ticket, kind)
    except DatabaseError:
        logger.warning("Orchestrator could not read recorded decisions", exc_info=True)
        return {"type": "answer", "answer_to": nonce, "verdict": "unavailable", "decisions": []}
    finally:
        close_thread_db_connections()
    return {
        "type": "answer",
        "answer_to": nonce,
        "verdict": "recorded" if decisions else "not_settled",
        "decisions": decisions,
    }


async def _serve(broker: live_mailbox.LiveMailboxBroker, identity: MailboxIdentity, stop: threading.Event) -> None:
    cursor = 0
    replies = 0
    reply_identity = broker.register(room="0", harness="orchestrator_reply", label="orchestrator reply")
    try:
        while not stop.is_set():
            client = MailboxClient(identity.socket_path, identity.token)
            try:
                page = await client.wait(after_id=cursor, timeout_seconds=1)
                peers = await client.peers() if page["messages"] else []
            except MailboxError:
                if broker.identity_of(identity.address) is None:
                    return
                logger.warning("Orchestrator mailbox read failed; retrying", exc_info=True)
                await asyncio.sleep(0.1)
                continue
            except Exception:
                logger.exception("Orchestrator mailbox read failed unexpectedly; retrying")
                await asyncio.sleep(0.1)
                continue
            cursor = page["next_cursor"]
            for message in page["messages"]:
                sender = next((peer for peer in peers if peer["address"] == message["from"]), None)
                try:
                    response = await asyncio.to_thread(
                        _answer, message["text"], sender.get("ticket", "") if sender else ""
                    )
                except Exception:
                    logger.exception("Orchestrator answer failed; continuing with later messages")
                    continue
                try:
                    await MailboxClient(reply_identity.socket_path, reply_identity.token).send(
                        message["from"], json.dumps(response), f"re:{message['id']}", timeout_seconds=0.1
                    )
                except MailboxError:
                    logger.warning("Orchestrator mailbox reply was not accepted", exc_info=True)
                replies += 1
                if replies >= MAX_SENT_KEYS // 2:
                    replacement = broker.register(room="0", harness="orchestrator_reply", label="orchestrator reply")
                    broker.unregister(reply_identity.address)
                    reply_identity = replacement
                    replies = 0
                    logger.info("Orchestrator reply sender rotated")
    finally:
        broker.unregister(reply_identity.address)
        broker.unregister(identity.address)
        close_thread_db_connections()


def start_orchestrator_peer(broker: live_mailbox.LiveMailboxBroker) -> threading.Thread:
    existing = [peer for peer in broker.identities() if peer["harness"] == "orchestrator"]
    if existing:
        thread = next(
            (thread for thread in threading.enumerate() if thread.name == f"t3-orchestrator-{id(broker)}"), None
        )
        if thread is not None:
            return thread
        broker.unregister(existing[0]["address"])
    identity = broker.register(room="0", harness="orchestrator", label="orchestrator")
    stop = threading.Event()
    thread = threading.Thread(
        target=lambda: asyncio.run(_serve(broker, identity, stop)), name=f"t3-orchestrator-{id(broker)}", daemon=True
    )

    def stop_peer() -> None:
        stop.set()
        thread.join(timeout=2)

    broker.add_close_callback(stop_peer)
    thread.start()
    return thread


def start_shared_orchestrator_peer() -> threading.Thread:
    return start_orchestrator_peer(live_mailbox.shared_broker())
