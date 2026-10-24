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
from teatree.core.models import PendingChatInjection, SessionCommand, Task, ThreadBinding
from teatree.core.models.task_handoff import release_operator_pause
from teatree.core.models.thread_binding import slack_command_id
from teatree.core.models.usage_window_state import OPERATOR_PAUSED_PREFIX
from teatree.core.worktree.survivor_notice import apply_stop_answer
from teatree.loop.inbound_reading import InboundIntent, InboundReader, InboundReading
from teatree.loop.question_binding import BoundAnswer, apply_bound_answer, bind_reply, configured_owner_id
from teatree.loop.slack_answer.control_verbs import control_verb
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
    if not unit.lead.mark_loop_replied(kind) and not unit.lead.claim_stale_loop_reply():
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


def _selected_task_id(text: str) -> int | None:
    selected = re.match(r"^#(\d+)(?:\s|$)", text.lstrip())
    return int(selected.group(1)) if selected is not None else None


def _pending_task(binding: ThreadBinding, text: str, *, paused_only: bool = False) -> Task | _AmbiguousSessions | None:
    tasks = list(Task.objects.filter(ticket_id=binding.ticket_id, status=Task.Status.PENDING).order_by("pk"))
    if paused_only:
        tasks = [
            task
            for task in tasks
            if task.not_before is not None
            and str(task.attempts.order_by("-pk").values_list("error", flat=True).first() or "").startswith(
                OPERATOR_PAUSED_PREFIX
            )
        ]
    selected = _selected_task_id(text)
    if selected is not None:
        tasks = [task for task in tasks if task.pk == selected]
    if len(tasks) == 1:
        return tasks[0]
    if not tasks:
        return None
    return _AmbiguousSessions(tuple(task.pk for task in tasks))


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
        selected = _selected_task_id(text)
        if selected is not None:
            selected_live = [row for row in matching if row["task"] == selected]
            if not selected_live and client.unreachable:
                msg = "live worker list is incomplete; ticket session ownership is unknown"
                raise LiveOfflineError(msg)
            if (
                not selected_live
                and Task.objects.filter(pk=selected, ticket_id=binding.ticket_id, status=Task.Status.PENDING).exists()
            ):
                return None
            if not selected_live:
                return _AmbiguousSessions(tuple(sorted(row["task"] for row in matching)))
            matching = selected_live
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
            send = client.pause if control_verb(unit.text) == "pause" else client.follow_up if idle else client.steer
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
    outcome = {
        "queued_followup": "queued follow-up (answering task)",
        "queued_resume": "queued follow-up (same task)",
        "answered_resume": "resume path requested from this answer",
        "queued": "queued for the same task's next run",
    }.get(command.outcome, command.outcome)
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


def _post_bound_reply(
    backend: "MessagingBackend", unit: "_Unit", report: "SlackAnswerReport", reply: str, *, kind: str
) -> bool:
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
    report.acked += 1
    return True


def _uncertain_question_reply(backend: "MessagingBackend", unit: "_Unit", report: "SlackAnswerReport") -> bool:
    return _post_bound_reply(
        backend,
        unit,
        report,
        "Answer recorded; receipt uncertain (unknown_delivery).",
        kind=PendingChatInjection.AnswerKind.QUESTION_REPLY,
    )


def _choice_reply(
    backend: "MessagingBackend", unit: "_Unit", report: "SlackAnswerReport", tasks: tuple[int, ...]
) -> bool:
    choices = " or ".join(f"#{task}" for task in tasks)
    return _post_bound_reply(
        backend,
        unit,
        report,
        f"Select a task for this ticket. Start your reply with {choices} to choose one.",
        kind=PendingChatInjection.AnswerKind.ACK,
    )


def _queue_pending_input(
    backend: "MessagingBackend", unit: "_Unit", report: "SlackAnswerReport", binding: ThreadBinding, task: Task
) -> bool:
    command, _created = SessionCommand.objects.get_or_create(
        command_id=slack_command_id(unit.overlay, unit.channel, unit.slack_ts),
        defaults={"ticket_id": binding.ticket_id, "task_id": task.pk, "text": unit.text},
    )
    if command.task_id != task.pk or command.text != unit.text:
        reply = f"Task {command.task_id}: rejected (duplicate_mismatch)."
    else:
        command.outcome = "queued"
        command.save(update_fields=["outcome"])
        reply = _command_reply(command)
    return _post_bound_reply(backend, unit, report, reply, kind=PendingChatInjection.AnswerKind.SESSION)


def _resume_pending_task(
    backend: "MessagingBackend", unit: "_Unit", report: "SlackAnswerReport", binding: ThreadBinding
) -> bool:
    existing = SessionCommand.objects.filter(
        command_id=slack_command_id(unit.overlay, unit.channel, unit.slack_ts)
    ).first()
    if (
        existing is not None
        and not existing.outcome
        and existing.ticket_id == binding.ticket_id
        and existing.text == unit.text
    ):
        released_task = Task.objects.filter(
            pk=existing.task_id, ticket_id=binding.ticket_id, status=Task.Status.PENDING, not_before__isnull=True
        ).first()
        if released_task is not None and str(
            released_task.attempts.order_by("-pk").values_list("error", flat=True).first() or ""
        ).startswith(OPERATOR_PAUSED_PREFIX):
            existing.outcome = "queued_resume"
            existing.save(update_fields=["outcome"])
            return _post_bound_reply(
                backend, unit, report, _command_reply(existing), kind=PendingChatInjection.AnswerKind.SESSION
            )
    selected = _pending_task(binding, unit.text, paused_only=True)
    if isinstance(selected, _AmbiguousSessions):
        return _choice_reply(backend, unit, report, selected.tasks)
    if selected is None:
        return _post_bound_reply(
            backend,
            unit,
            report,
            "Resume rejected (turn_ended): no operator-paused task is pending.",
            kind=PendingChatInjection.AnswerKind.ACK,
        )
    command, _created = SessionCommand.objects.get_or_create(
        command_id=slack_command_id(unit.overlay, unit.channel, unit.slack_ts),
        defaults={"ticket_id": binding.ticket_id, "task_id": selected.pk, "text": unit.text},
    )
    if command.task_id != selected.pk or command.text != unit.text:
        reply = f"Task {command.task_id}: rejected (duplicate_mismatch)."
    else:
        if not command.outcome:
            released = release_operator_pause(selected)
            selected.refresh_from_db()
            command.outcome = "queued_resume" if released or selected.not_before is None else "rejected"
            command.code = "" if command.outcome == "queued_resume" else "turn_ended"
            command.save(update_fields=["outcome", "code"])
        reply = _command_reply(command)
    return _post_bound_reply(backend, unit, report, reply, kind=PendingChatInjection.AnswerKind.SESSION)


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
        if unit.lead.answer_kind == PendingChatInjection.AnswerKind.QUESTION_REPLY and bound_answer is None:
            return _uncertain_question_reply(backend, unit, report)
        return False
    if not command.outcome:
        return False
    if command.ticket_id != binding.ticket_id or command.text != unit.text:
        reply = f"Task {command.task_id}: rejected (duplicate_mismatch)."
    else:
        reply = _command_reply(command)
        _apply_live_answer(command, bound_answer, backend)
    kind = {
        "queued_followup": PendingChatInjection.AnswerKind.DELEGATED,
        "answered_resume": PendingChatInjection.AnswerKind.QUESTION_REPLY,
    }.get(command.outcome, PendingChatInjection.AnswerKind.SESSION)
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
    elif command.outcome == "answered_resume":
        report.answered_question += 1
    else:
        report.steered += 1
    return True


def _no_live_session(
    backend: "MessagingBackend", unit: "_Unit", report: "SlackAnswerReport", binding: ThreadBinding, verb: str | None
) -> bool:
    command_id = slack_command_id(unit.overlay, unit.channel, unit.slack_ts)
    unsettled = SessionCommand.objects.filter(command_id=command_id, outcome="").first()
    if unsettled is not None:
        if unsettled.ticket_id != binding.ticket_id or unsettled.text != unit.text:
            return _post_bound_reply(
                backend,
                unit,
                report,
                f"Task {unsettled.task_id}: rejected (duplicate_mismatch).",
                kind=PendingChatInjection.AnswerKind.ACK,
            )
        unsettled.outcome = "unknown_delivery"
        unsettled.save(update_fields=["outcome"])
        return _replay_bound_command(backend, unit, report, binding, None)
    if unit.lead.answer_kind == PendingChatInjection.AnswerKind.SESSION:
        _unmark_unit_loop_replied(unit)
    if verb == "pause":
        return _post_bound_reply(
            backend,
            unit,
            report,
            "Pause rejected (turn_ended): no live task is running.",
            kind=PendingChatInjection.AnswerKind.ACK,
        )
    pending = _pending_task(binding, unit.text)
    if isinstance(pending, _AmbiguousSessions):
        return _choice_reply(backend, unit, report, pending.tasks)
    if pending is not None:
        return _queue_pending_input(backend, unit, report, binding, pending)
    return False


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
    verb = control_verb(unit.text)
    if verb == "resume":
        return _resume_pending_task(backend, unit, report, binding)
    session = _live_session(binding, unit.text)
    if bound_answer is not None and (
        not isinstance(session, _BoundLiveSession) or session.facts["task"] != bound_answer.question.parked_task_id
    ):
        return False
    if isinstance(session, _AmbiguousSessions):
        return _choice_reply(backend, unit, report, session.tasks)
    if session is None:
        return _no_live_session(backend, unit, report, binding, verb)
    return _apply_live_session(backend, unit, report, session, _LiveInput(reader(unit.text), bound_answer))


def _apply_live_answer(command: SessionCommand, bound_answer: BoundAnswer | None, backend: "MessagingBackend") -> None:
    if (
        bound_answer is not None
        and bound_answer.question.parked_task_id == command.task_id
        and command.outcome in {"accepted_current_turn", "accepted_new_turn", "queued"}
    ):
        apply_bound_answer(bound_answer, backend=backend, on_answer=apply_stop_answer, resume_task=False)


def _apply_live_session(
    backend: "MessagingBackend",
    unit: "_Unit",
    report: "SlackAnswerReport",
    session: _BoundLiveSession,
    route: _LiveInput,
) -> bool:
    task_pk = session.facts["task"]
    pause = control_verb(unit.text) == "pause"
    inspect = (
        not pause
        and route.bound_answer is None
        and route.reading.intent is InboundIntent.QUESTION
        and route.reading.answerable
    )
    if not inspect and not pause and not session.facts["steerable"] and session.facts["state"] != "idle":
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
