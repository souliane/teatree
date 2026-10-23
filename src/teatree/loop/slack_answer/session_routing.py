"""Durable delivery of Slack thread input to a live ticket session."""

from dataclasses import dataclass
from operator import itemgetter
from typing import TYPE_CHECKING

from teatree.agents.live_client import (
    LiveClient,
    LiveDeliveryUnknownError,
    LiveOfflineError,
    LiveRefusedError,
    WorkerSessionFacts,
)
from teatree.core.models import PendingChatInjection, SessionCommand, ThreadBinding
from teatree.core.models.thread_binding import slack_command_id
from teatree.loop.inbound_reading import InboundIntent, InboundReading
from teatree.loop.question_binding import configured_owner_id
from teatree.loop.slack_answer.thread_readback import ExpectedReply, bot_reply_present_in_thread

if TYPE_CHECKING:
    from teatree.core.backend_protocols import MessagingBackend
    from teatree.loop.slack_answer.cycle import SlackAnswerReport, _Unit


def _mark_unit_loop_replied(unit: "_Unit", kind: str) -> bool:
    """CAS ``mark_loop_replied`` on the lead; stamp every coalesced row to match.

    The lead's CAS is the single idempotency boundary (the row that wins
    creates the side effect). The follow-up rows are stamped best-effort
    so they drop out of ``loop_unreplied()`` together with the lead — one
    logical turn, one loop reply, no orphaned follow-up re-processed alone.
    Stamps only ``loop_replied_at`` (#1075); never ``answered_at``, the agent's own reply.
    """
    if not unit.lead.mark_loop_replied(kind):
        return False
    for follow in unit.rows[1:]:
        follow.mark_loop_replied(kind)
    return True


def _unmark_unit_loop_replied(unit: "_Unit") -> None:
    """Release the whole unit's loop-reply claim — the rollback of :func:`_mark_unit_loop_replied`."""
    for row in unit.rows:
        row.unmark_loop_replied()


@dataclass(frozen=True, slots=True)
class _BoundLiveSession:
    binding: ThreadBinding
    client: LiveClient
    facts: WorkerSessionFacts


def _live_session(binding: ThreadBinding) -> _BoundLiveSession | None:
    client = LiveClient()
    try:
        sessions = client.sessions()
    except LiveOfflineError:
        if client.workers():
            raise
        return None
    matching = [row for row in sessions if row["ticket"] == binding.ticket_id]
    if matching:
        return _BoundLiveSession(binding, client, max(matching, key=itemgetter("task")))
    if client.unreachable:
        msg = "live worker list is incomplete; ticket session ownership is unknown"
        raise LiveOfflineError(msg)
    return None


def _direction_reply(client: LiveClient, unit: "_Unit", binding: ThreadBinding, task_pk: int, *, idle: bool) -> str:
    command_id = slack_command_id(unit.overlay, unit.channel, unit.slack_ts)
    command, created = SessionCommand.objects.get_or_create(
        command_id=command_id,
        defaults={"ticket_id": binding.ticket_id, "task_id": task_pk, "text": unit.text, "outcome": "unknown_delivery"},
    )
    if command.ticket_id != binding.ticket_id or command.text != unit.text:
        return f"Task {task_pk}: rejected (duplicate_mismatch)."
    if created:
        try:
            send = client.follow_up if idle else client.steer
            receipt = send(task_pk, unit.text, command_id=command_id)
        except LiveOfflineError:
            command.delete()
            raise
        except LiveDeliveryUnknownError:
            receipt = {"outcome": "unknown_delivery", "code": None, "accepted_at": None}
        except LiveRefusedError as exc:
            receipt = {"outcome": "rejected", "code": exc.code, "accepted_at": None}
        command.outcome = str(receipt["outcome"])
        command.code = str(receipt.get("code") or "")
        command.accepted_at = str(receipt.get("accepted_at") or "")
        command.save(update_fields=["outcome", "code", "accepted_at"])
    elif not command.outcome:
        command.outcome = "unknown_delivery"
        command.save(update_fields=["outcome"])
    return _command_reply(command)


def _command_reply(command: SessionCommand) -> str:
    if not command.outcome:
        command.outcome = "unknown_delivery"
        command.save(update_fields=["outcome"])
    outcome = "queued follow-up (answering task)" if command.outcome == "queued_followup" else command.outcome
    code = f" ({command.code})" if command.code else ""
    return f"Task {command.task_id}: {outcome}{code}."


def _post_live_reply(backend: "MessagingBackend", unit: "_Unit", reply: str) -> bool:
    root = unit.lead.thread_ts
    floor = unit.rows[-1].slack_ts
    if bot_reply_present_in_thread(
        backend, channel=unit.channel, thread_root=root, after_ts=floor, expected_ts=ExpectedReply(None, reply)
    ):
        return True
    posted = backend.post_reply(channel=unit.channel, ts=unit.slack_ts, text=reply)
    posted_ts = posted.get("ts")
    return bool(
        posted.get("ok") is True
        and isinstance(posted_ts, str)
        and bot_reply_present_in_thread(
            backend,
            channel=unit.channel,
            thread_root=root,
            after_ts=floor,
            expected_ts=ExpectedReply(posted_ts, reply),
        )
    )


def _replay_bound_command(
    backend: "MessagingBackend", unit: "_Unit", report: "SlackAnswerReport", binding: ThreadBinding
) -> bool:
    command = SessionCommand.objects.filter(
        command_id=slack_command_id(unit.overlay, unit.channel, unit.slack_ts)
    ).first()
    if command is None:
        return False
    if command.ticket_id != binding.ticket_id or command.text != unit.text:
        reply = f"Task {command.task_id}: rejected (duplicate_mismatch)."
    else:
        reply = _command_reply(command)
    kind = (
        PendingChatInjection.AnswerKind.DELEGATED
        if command.outcome == "queued_followup"
        else PendingChatInjection.AnswerKind.SESSION
    )
    if not _mark_unit_loop_replied(unit, kind):
        return True
    try:
        if not _post_live_reply(backend, unit, reply):
            _unmark_unit_loop_replied(unit)
            return True
    except Exception:
        _unmark_unit_loop_replied(unit)
        raise
    for row in unit.rows:
        row.observe_confirmed_loop_reply()
    if command.outcome == "queued_followup":
        report.dispatched += 1
    else:
        report.steered += 1
    return True


def _live_receipt(
    backend: "MessagingBackend",
    unit: "_Unit",
    reading: InboundReading,
    report: "SlackAnswerReport",
    binding: ThreadBinding | None,
) -> bool:
    if binding is None:
        return False
    if _replay_bound_command(backend, unit, report, binding):
        return True
    if (session := _live_session(binding)) is None:
        return False
    return _apply_live_session(backend, unit, reading, report, session)


def _apply_live_session(
    backend: "MessagingBackend",
    unit: "_Unit",
    reading: InboundReading,
    report: "SlackAnswerReport",
    session: _BoundLiveSession,
) -> bool:
    task_pk = session.facts["task"]
    inspect = reading.intent is InboundIntent.QUESTION and reading.answerable
    if not inspect and not session.facts["steerable"] and session.facts["state"] != "idle":
        return False
    if not _mark_unit_loop_replied(unit, PendingChatInjection.AnswerKind.SESSION):
        return True
    try:
        if inspect:
            details = session.client.inspect(task_pk)
            reply = f"Task {task_pk}: {details['state']}; {details['tool_calls']} tool calls."
        else:
            reply = _direction_reply(
                session.client, unit, session.binding, task_pk, idle=session.facts["state"] == "idle"
            )
        if not _post_live_reply(backend, unit, reply):
            _unmark_unit_loop_replied(unit)
            return True
    except Exception:
        _unmark_unit_loop_replied(unit)
        raise
    for row in unit.rows:
        row.observe_confirmed_loop_reply()
    if inspect:
        report.inspected += 1
    else:
        report.steered += 1
    return True


def _reject_foreign_bound_unit(
    backend: "MessagingBackend", unit: "_Unit", report: "SlackAnswerReport", binding: ThreadBinding | None
) -> bool:
    if binding is None:
        return False
    owner_id = configured_owner_id(backend)
    if owner_id and unit.lead.user_id == owner_id:
        return False
    if _mark_unit_loop_replied(unit, PendingChatInjection.AnswerKind.ACK):
        report.acked += 1
    return True
