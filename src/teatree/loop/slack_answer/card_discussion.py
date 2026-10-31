"""Route an owner's open-card discussion to its live task or coordinator."""

import asyncio
import logging
import threading
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from django.utils import timezone

from teatree.agents import live_mailbox
from teatree.agents.coordinator_escalation import tell_live_coordinator
from teatree.agents.data_fence import data_rule, fenced
from teatree.agents.live_control import PeerIdentity
from teatree.agents.live_delivery import MAX_SENT_KEYS, MailboxIdentity
from teatree.agents.live_mailbox_client import MailboxClient, MailboxError
from teatree.core.answering.escalation import open_request, resume_coordinator
from teatree.core.models import PendingChatInjection, TaskMintRefusedError, ThreadBinding
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from teatree.core.models.task_handoff import schedule_card_discussion
from teatree.loop.question_binding import AmbiguousCards, BoundAnswer, CardThreadStop, UnmatchedCardThread
from teatree.loop.slack_answer.coalescing import _Unit
from teatree.loop.slack_answer.control_verbs import control_verb
from teatree.loop.slack_answer.session_routing import _mark_unit_loop_replied, _unmark_unit_loop_replied

if TYPE_CHECKING:
    from teatree.core.backend_protocols import MessagingBackend
    from teatree.loop.slack_answer.cycle import SlackAnswerReport

logger = logging.getLogger(__name__)
UNROUTED_AUDIT_ACTION = "reply_unrouted"
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
            and peer["harness"] not in {live_mailbox.OPERATOR_HARNESS, "orchestrator"}
        ]
        if parked_task_id is not None:
            exact = [peer for peer in candidates if peer["label"].endswith(f" task {parked_task_id}")]
            if len(exact) == 1:
                return exact[0]
        if len(candidates) == 1:
            return candidates[0]
    coordinators = [
        peer for peer in peers if peer["harness"] == live_mailbox.OPERATOR_HARNESS and peer.get("can_receive", True)
    ]
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
    except TaskMintRefusedError:
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


def _tell_owner_nobody_is_listening(backend: "MessagingBackend", unit: _Unit, question: DeferredQuestion) -> None:
    reply_timestamps = [row.slack_ts for row in unit.rows]
    audits = DeferredQuestionAudit.objects.filter(
        question=question, action=UNROUTED_AUDIT_ACTION, note__in=reply_timestamps
    )
    if audits.exists():
        audits.update(resolved_at=timezone.now())
        return
    posted = backend.post_reply(channel=unit.channel, ts=unit.slack_ts, text=NOBODY_LISTENING)
    if posted.get("ok") is True:
        DeferredQuestionAudit.objects.bulk_create(
            [
                DeferredQuestionAudit(
                    question=question, action=UNROUTED_AUDIT_ACTION, note=timestamp, resolver_id="card_discussion"
                )
                for timestamp in reply_timestamps
            ]
        )


def _reply_message(request: DeferredQuestion, unit: _Unit) -> str:
    return (
        f"The owner replied in the Slack thread {unit.lead.thread_ts} on channel {unit.channel} while escalation "
        f"{request.pk} is open.\n{fenced('OWNER MESSAGE', unit.text)}\n{data_rule('OWNER MESSAGE')}\n"
        f"Answer in the same thread; use `t3 <overlay> notify reply '<answer>' "
        f"--idempotency-key escalation-reply:{unit.slack_ts}:reply --thread-ts {unit.lead.thread_ts}`."
    )


def _route_to_coordinator(request: DeferredQuestion, ticket_id: int, unit: _Unit) -> bool:
    key = f"escalation-reply:{unit.overlay}:{unit.channel}:{unit.slack_ts}"
    message = _reply_message(request, unit)
    try:
        broker = live_mailbox.shared_broker()
        with _SENDER_LOCK:
            identity = _router_sender(broker, key)
            told = asyncio.run(
                tell_live_coordinator(
                    MailboxClient(identity.socket_path, identity.token), ticket_id=ticket_id, text=message, key=key
                )
            )
    except (MailboxError, OSError, RuntimeError, ValueError):
        logger.warning("Escalation %s reply was not accepted by a live coordinator", request.pk, exc_info=True)
        told = False
    return told or resume_coordinator(ticket_id, event_id=key, message=message) is not None


def _route_open_escalation(unit: _Unit, report: "SlackAnswerReport") -> bool:
    """Send the owner's message to the coordinator while the thread's ticket has an open escalation request."""
    if not unit.lead.thread_ts or control_verb(unit.text) is not None:
        return False
    ticket_id = (
        ThreadBinding.objects.filter(overlay=unit.overlay, channel=unit.channel, thread_ts=unit.lead.thread_ts)
        .values_list("ticket_id", flat=True)
        .first()
    )
    request = open_request(ticket_id) if ticket_id is not None else None
    if request is None or not _mark_unit_loop_replied(unit, PendingChatInjection.AnswerKind.DELEGATED):
        return request is not None
    try:
        routed = _route_to_coordinator(request, ticket_id, unit)
    except Exception:
        _unmark_unit_loop_replied(unit)
        raise
    if not routed:
        _unmark_unit_loop_replied(unit)
        return False
    report.dispatched += 1
    return True


def route_card_result(
    backend: "MessagingBackend",
    unit: _Unit,
    result: UnmatchedCardThread | AmbiguousCards | BoundAnswer | None,
    report: "SlackAnswerReport",
) -> bool:
    if result is None:
        return _route_open_escalation(unit, report)
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
        if question is not None:
            _tell_owner_nobody_is_listening(backend, unit, question)
        return True
    report.dispatched += 1
    return True
