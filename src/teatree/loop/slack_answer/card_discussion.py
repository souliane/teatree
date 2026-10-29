"""Route an owner's open-card discussion to its live task or attended coordinator."""

import asyncio
import logging
import threading
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from teatree.agents import live_mailbox
from teatree.agents.live_control import PeerIdentity
from teatree.agents.live_delivery import MAX_SENT_KEYS, MailboxIdentity
from teatree.agents.live_mailbox_client import MailboxClient, MailboxError
from teatree.core.models import NoPlanArtifactError, PendingChatInjection
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.task_handoff import schedule_card_discussion
from teatree.loop.question_binding import AmbiguousCards, BoundAnswer, CardThreadStop, UnmatchedCardThread
from teatree.loop.slack_answer.coalescing import _Unit
from teatree.loop.slack_answer.session_routing import (
    _mark_unit_loop_replied,
    _post_live_reply,
    _unmark_unit_loop_replied,
)

if TYPE_CHECKING:
    from teatree.core.backend_protocols import MessagingBackend
    from teatree.loop.slack_answer.cycle import SlackAnswerReport

logger = logging.getLogger(__name__)
NOBODY_LISTENING = (
    "No agent is working on this card and no coordinator session is connected, so nobody can answer here yet. "
    "Your message is kept and goes to the first coordinator session that connects, which will answer in this thread. "
    "The card stays open."
)
_SENDER_LOCK = threading.Lock()
_SENDERS: WeakKeyDictionary[live_mailbox.LiveMailboxBroker, tuple[MailboxIdentity, set[str]]] = WeakKeyDictionary()


def reset_card_discussion_senders() -> None:
    with _SENDER_LOCK:
        for broker, (identity, _keys) in list(_SENDERS.items()):
            broker.unregister(identity.address)
        _SENDERS.clear()


def _router_sender(broker: live_mailbox.LiveMailboxBroker, key: str) -> MailboxIdentity:
    state = _SENDERS.get(broker)
    if (
        state is None
        or broker.identity_of(state[0].address) is None
        or (len(state[1]) >= MAX_SENT_KEYS // 2 and key not in state[1])
    ):
        replacement = broker.register(room="0", harness="card_router", label="card discussion router")
        if state is not None:
            broker.unregister(state[0].address)
        state = (replacement, set())
        _SENDERS[broker] = state
    state[1].add(key)
    return state[0]


def _responsible_ticket(row: DeferredQuestion) -> int | None:
    parked_task = row.parked_task
    if parked_task is not None:
        return parked_task.ticket_id
    task_session = row.task_session
    if task_session is not None:
        return task_session.ticket_id
    return None


def _choose_peer(peers: list[PeerIdentity], ticket_id: int | None, parked_task_id: int | None) -> PeerIdentity | None:
    if ticket_id is not None:
        candidates = [
            peer
            for peer in peers
            if peer.get("ticket") == str(ticket_id)
            and peer.get("can_receive", True)
            and peer["harness"] not in {"interactive", "orchestrator"}
        ]
        if parked_task_id is not None:
            exact = [peer for peer in candidates if peer["label"].endswith(f" task {parked_task_id}")]
            if len(exact) == 1:
                return exact[0]
        if len(candidates) == 1:
            return candidates[0]
    coordinators = [peer for peer in peers if peer["harness"] == "interactive" and peer.get("can_receive", True)]
    return coordinators[0] if len(coordinators) == 1 else None


def _subject(row: DeferredQuestion) -> str:
    subject = row.evidence.get("subject") if isinstance(row.evidence, dict) else None
    if isinstance(subject, str) and subject:
        return subject
    ticket_id = _responsible_ticket(row)
    if ticket_id is not None:
        parked_task = row.parked_task
        task_session = row.task_session
        ticket = (
            parked_task.ticket if parked_task is not None else task_session.ticket if task_session is not None else None
        )
        if ticket is not None and ticket.issue_url:
            return ticket.issue_url
    return row.question


def _message(row: DeferredQuestion, unit: _Unit) -> str:
    return (
        f"The owner is discussing open card {row.pk}. Subject: {_subject(row)}. "
        f"Owner's message: {unit.text}\n"
        f"Answer in the same Slack thread {unit.lead.thread_ts} on channel {unit.channel}; "
        f"use `t3 <overlay> notify reply '<answer>' --idempotency-key card-discussion:{unit.slack_ts}:reply "
        f"--thread-ts {unit.lead.thread_ts}`. Leave the card pending until an option or explicit decision."
    )


async def _send(
    client: MailboxClient, *, ticket_id: int | None, parked_task_id: int | None, message: str, key: str
) -> tuple[bool, bool]:
    peers = await client.peers()
    recipient = _choose_peer(peers, ticket_id, parked_task_id)
    if recipient is None:
        return False, False
    receipt = await client.send(
        recipient["address"],
        message,
        key,
        timeout_seconds=2,
    )
    return receipt["outcome"].startswith("accepted_"), True


def _resume_responsible_task(row: DeferredQuestion, unit: _Unit) -> bool:
    parked_task = row.parked_task
    if parked_task is None:
        return False
    try:
        schedule_card_discussion(
            parked_task,
            event_id=f"{unit.overlay}:{unit.channel}:{unit.slack_ts}",
            message=_message(row, unit),
        )
    except NoPlanArtifactError:
        logger.warning("Card %s discussion could not resume its responsible task", row.pk)
        return False
    return True


def route_card_discussion(row: DeferredQuestion, unit: _Unit) -> bool:
    """Deliver one free-text reply without resolving the question or posting a canned refusal."""
    key = f"card-discussion:{unit.overlay}:{unit.channel}:{unit.slack_ts}"
    try:
        broker = live_mailbox.shared_broker()
        with _SENDER_LOCK:
            identity = _router_sender(broker, key)
            accepted, _peer_found = asyncio.run(
                _send(
                    MailboxClient(identity.socket_path, identity.token),
                    ticket_id=_responsible_ticket(row),
                    parked_task_id=row.parked_task_id,
                    message=_message(row, unit),
                    key=key,
                )
            )
        return accepted or _resume_responsible_task(row, unit)
    except (MailboxError, OSError, RuntimeError, ValueError):
        logger.warning("Card %s discussion was not accepted by a live peer", row.pk, exc_info=True)
        return _resume_responsible_task(row, unit)


def route_card_result(
    backend: "MessagingBackend",
    unit: _Unit,
    result: UnmatchedCardThread | AmbiguousCards | BoundAnswer | None,
    report: "SlackAnswerReport",
) -> bool:
    if not isinstance(result, UnmatchedCardThread) or result.reason is not CardThreadStop.OPTIONS_ONLY:
        return False
    if not _mark_unit_loop_replied(unit, PendingChatInjection.AnswerKind.DELEGATED):
        return True
    try:
        question = DeferredQuestion.objects.filter(pk=result.question_id).first()
        routed = question is not None and route_card_discussion(question, unit)
    except Exception:
        _unmark_unit_loop_replied(unit)
        raise
    if not routed:
        _unmark_unit_loop_replied(unit)
        _post_live_reply(backend, unit, NOBODY_LISTENING)
        return True
    report.dispatched += 1
    return True
