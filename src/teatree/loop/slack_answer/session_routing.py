"""Durable delivery of Slack thread input to a live ticket session."""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import transaction

from teatree.agents.live_client import (
    LiveClient,
    LiveDeliveryUnknownError,
    LiveOfflineError,
    LiveRefusedError,
    WorkerSessionFacts,
)
from teatree.core.models import PendingChatInjection, SessionCommand, ThreadBinding
from teatree.core.models.thread_binding import slack_command_id
from teatree.loop.inbound_reading import InboundIntent, InboundReader, InboundReading
from teatree.loop.question_binding import BoundAnswer, apply_bound_answer, bind_reply, configured_owner_id
from teatree.loop.slack_answer.thread_readback import ExpectedReply, bot_reply_present_in_thread

if TYPE_CHECKING:
    from teatree.core.backend_protocols import MessagingBackend
    from teatree.loop.slack_answer.cycle import SlackAnswerReport, _Unit

SLACK_STEER_WAIT_SECONDS = 10.0


def _mark_unit_loop_replied(unit: "_Unit", kind: str) -> bool:
    """CAS ``mark_loop_replied`` on the lead; stamp every coalesced row to match.

    The lead's CAS is the single idempotency boundary (the row that wins
    creates the side effect). The follow-up rows are stamped best-effort
    so they drop out of ``loop_unreplied()`` together with the lead — one
    logical turn, one loop reply, no orphaned follow-up re-processed alone.
    Stamps only ``loop_replied_at`` (#1075); never ``answered_at``, the agent's own reply.
    """
    if not unit.lead.mark_loop_replied(kind) and not unit.lead.claim_stale_session_reply():
        return False
    for follow in unit.rows[1:]:
        follow.mark_loop_replied(kind)
    return True


def _confirm_unit_loop_replied(unit: "_Unit") -> None:
    with transaction.atomic():
        for row in unit.rows:
            row.observe_confirmed_loop_reply()


def _unmark_unit_loop_replied(unit: "_Unit") -> None:
    """Release the whole unit's loop-reply claim — the rollback of :func:`_mark_unit_loop_replied`."""
    for row in unit.rows:
        row.unmark_loop_replied()


@dataclass(frozen=True, slots=True)
class _BoundLiveSession:
    binding: ThreadBinding
    client: LiveClient
    facts: WorkerSessionFacts


@dataclass(frozen=True, slots=True)
class _LiveInput:
    reading: InboundReading
    bound_answer: BoundAnswer | None


@dataclass(frozen=True, slots=True)
class _AmbiguousSessions:
    tasks: tuple[int, ...]


def _live_session(binding: ThreadBinding, text: str) -> _BoundLiveSession | _AmbiguousSessions | None:
    client = LiveClient()
    try:
        sessions = client.sessions()
    except LiveOfflineError:
        if client.workers():
            raise
        return None
    matching = [row for row in sessions if row["ticket"] == binding.ticket_id]
    if matching:
        selected = re.match(r"^#(\d+)(?:\s|$)", text)
        if selected is not None:
            matching = [row for row in matching if row["task"] == int(selected.group(1))]
        if len(matching) == 1:
            return _BoundLiveSession(binding, client, matching[0])
        return _AmbiguousSessions(tuple(sorted(row["task"] for row in sessions if row["ticket"] == binding.ticket_id)))
    if client.unreachable:
        msg = "live worker list is incomplete; ticket session ownership is unknown"
        raise LiveOfflineError(msg)
    return None


def _direction_reply(client: LiveClient, unit: "_Unit", binding: ThreadBinding, task_pk: int, *, idle: bool) -> str:
    command_id = slack_command_id(unit.overlay, unit.channel, unit.slack_ts)
    command, created = SessionCommand.objects.get_or_create(
        command_id=command_id,
        defaults={"ticket_id": binding.ticket_id, "task_id": task_pk, "text": unit.text},
    )
    if command.ticket_id != binding.ticket_id or command.text != unit.text:
        return f"Task {task_pk}: rejected (duplicate_mismatch)."
    if command.task_id != task_pk:
        msg = "unsettled session command belongs to a different task"
        raise LiveOfflineError(msg)
    if created or not command.outcome:
        try:
            send = client.follow_up if idle else client.steer
            receipt = send(task_pk, unit.text, command_id=command_id, wait=SLACK_STEER_WAIT_SECONDS)
        except LiveOfflineError:
            if created:
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
    backend: "MessagingBackend",
    unit: "_Unit",
    report: "SlackAnswerReport",
    binding: ThreadBinding,
    bound_answer: BoundAnswer | None,
) -> bool:
    command = SessionCommand.objects.filter(
        command_id=slack_command_id(unit.overlay, unit.channel, unit.slack_ts)
    ).first()
    if command is None:
        return False
    if not command.outcome:
        return False
    if command.ticket_id != binding.ticket_id or command.text != unit.text:
        reply = f"Task {command.task_id}: rejected (duplicate_mismatch)."
    else:
        reply = _command_reply(command)
        _apply_live_answer(command, bound_answer, backend)
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
    _confirm_unit_loop_replied(unit)
    if command.outcome == "queued_followup":
        report.dispatched += 1
    else:
        report.steered += 1
    return True


def _live_receipt(
    backend: "MessagingBackend",
    unit: "_Unit",
    report: "SlackAnswerReport",
    binding: ThreadBinding,
    reader: InboundReader,
) -> bool:
    bound_answer = bind_reply(unit.lead, reader=reader, owner_user_id=configured_owner_id(backend), text=unit.text)
    if _replay_bound_command(backend, unit, report, binding, bound_answer):
        return True
    session = _live_session(binding, unit.text)
    if isinstance(session, _AmbiguousSessions):
        if not _mark_unit_loop_replied(unit, PendingChatInjection.AnswerKind.ACK):
            return True
        choices = " or ".join(f"#{task}" for task in session.tasks)
        reply = f"Several tasks are live for this ticket. Start your reply with {choices} to choose one."
        try:
            if not _post_live_reply(backend, unit, reply):
                _unmark_unit_loop_replied(unit)
                return True
        except Exception:
            _unmark_unit_loop_replied(unit)
            raise
        _confirm_unit_loop_replied(unit)
        report.acked += 1
        return True
    if session is None:
        command_id = slack_command_id(unit.overlay, unit.channel, unit.slack_ts)
        if SessionCommand.objects.filter(command_id=command_id).exists():
            msg = "unsettled session command has no reachable owner"
            raise LiveOfflineError(msg)
        if unit.lead.answer_kind == PendingChatInjection.AnswerKind.SESSION:
            _unmark_unit_loop_replied(unit)
        return False
    return _apply_live_session(backend, unit, report, session, _LiveInput(reader(unit.text), bound_answer))


def _apply_live_answer(command: SessionCommand, bound_answer: BoundAnswer | None, backend: "MessagingBackend") -> None:
    if (
        bound_answer is not None
        and bound_answer.question.parked_task_id == command.task_id
        and command.outcome in {"accepted_current_turn", "accepted_new_turn"}
    ):
        apply_bound_answer(bound_answer, backend=backend, resume_task=False)


def _apply_live_session(
    backend: "MessagingBackend",
    unit: "_Unit",
    report: "SlackAnswerReport",
    session: _BoundLiveSession,
    route: _LiveInput,
) -> bool:
    task_pk = session.facts["task"]
    inspect = route.bound_answer is None and route.reading.intent is InboundIntent.QUESTION and route.reading.answerable
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
            command = SessionCommand.objects.get(command_id=slack_command_id(unit.overlay, unit.channel, unit.slack_ts))
            _apply_live_answer(command, route.bound_answer, backend)
        if not _post_live_reply(backend, unit, reply):
            _unmark_unit_loop_replied(unit)
            return True
    except Exception:
        _unmark_unit_loop_replied(unit)
        raise
    _confirm_unit_loop_replied(unit)
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
