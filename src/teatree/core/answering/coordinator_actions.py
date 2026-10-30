"""Run the coordinator's proposed actions: the closed table, then the owner's four limits, then one write (#5685).

The answering phase reads untrusted thread text and holds no write tool, so it can only RETURN an
``actions`` list. Each entry runs here, in order, only when its kind is in the table, it declares
no argument beyond the kind's own, and the evidence the owner's own message carries still holds.
A refusal runs nothing and is recorded with its reason; the owner sees each confirmed receipt or
refusal, never a model claim that an action happened.
"""

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from django.db import transaction

from teatree.core.managers_issue_match import reference_issue_q
from teatree.core.modelkit.coordinator_action import CoordinatorActionKind, action_refusal
from teatree.core.models import Task, ThreadBinding, Ticket
from teatree.core.models.task_enqueue import enqueue_phase_task
from teatree.url_classify import find_forge_refs
from teatree.utils.url_slug import is_slack_conversation_anchor

logger = logging.getLogger(__name__)

AUDIT_KEY = "coordinator_actions"


class ActionRefusedError(Exception):
    """The action is well-formed but its evidence does not hold; nothing was written."""


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    executed: bool
    detail: str


def _owner_message(conversation: Ticket) -> str:
    return str(conversation.slack_answer_context().get("question") or "")


def _tracked_ticket(conversation: Ticket, named: str) -> Ticket:
    target = Ticket.objects.in_flight(conversation.overlay).filter(reference_issue_q(named)).order_by("-pk").first()
    if target is None or not target.is_admissible():
        message = "no open tracked ticket matches it"
        raise ActionRefusedError(message)
    return target


def _bind_thread(task: Task, arguments: Mapping[str, object]) -> str:
    conversation = task.ticket
    if not is_slack_conversation_anchor(conversation.issue_url):
        message = "the task's ticket is not a Slack conversation"
        raise ActionRefusedError(message)
    named = str(arguments.get("ticket") or "")
    if named not in find_forge_refs(_owner_message(conversation)):
        message = "the owner's message does not name that ticket"
        raise ActionRefusedError(message)
    target = _tracked_ticket(conversation, named)
    with transaction.atomic():
        bindings = ThreadBinding.objects.select_for_update().filter(ticket=conversation)
        if not bindings.exists():
            message = "this thread is not bound"
            raise ActionRefusedError(message)
        bindings.update(ticket=target)
    return f"Bound this thread to <{target.issue_url}|#{target.ticket_number}>."


def _queue_task(task: Task, arguments: Mapping[str, object]) -> str:
    conversation = task.ticket
    if not conversation.is_conversation():
        message = "the task's ticket is not a conversation"
        raise ActionRefusedError(message)
    named = arguments.get("ticket")
    if not isinstance(named, str) or not named:
        message = "queue_task needs a ticket URL"
        raise ActionRefusedError(message)
    if named == conversation.issue_url:
        target = conversation
    else:
        recorded = str(conversation.slack_answer_context().get("work_issue_url") or "")
        if named != recorded and named not in find_forge_refs(_owner_message(conversation)):
            message = "the conversation does not identify that work ticket"
            raise ActionRefusedError(message)
        target = _tracked_ticket(conversation, named)
    with transaction.atomic():
        target = Ticket.objects.select_for_update().get(pk=target.pk)
        if target.state not in Ticket.EARLY_STATES:
            message = f"planning is not available from {target.state}"
            raise ActionRefusedError(message)
        if not Task.loop_dispatched(role=target.role, phase="planning"):
            message = f"no sub-agent plans a {target.role} ticket"
            raise ActionRefusedError(message)
        queued = (
            Task.objects.in_flight_for_phase(target.overlay, "planning").filter(ticket=target).order_by("pk").first()
        )
        if queued is None:
            queued = enqueue_phase_task(
                ticket=target,
                phase="planning",
                reason="Plan this ticket from its recorded context.",
                agent_id="coordinator",
            )
    return f"Planning is queued as TODO-{queued.pk}."


_PROCESSORS: Mapping[CoordinatorActionKind, Callable[[Task, Mapping[str, object]], str]] = {
    CoordinatorActionKind.BIND_THREAD: _bind_thread,
    CoordinatorActionKind.QUEUE_TASK: _queue_task,
}


def _key(task: Task, kind: str, arguments: Mapping[str, object]) -> str:
    identity = json.dumps([task.pk, kind, sorted(arguments.items())], default=str)
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


def _run_one(task: Task, request: object) -> ActionOutcome | None:
    if not isinstance(request, dict):
        return ActionOutcome(executed=False, detail="an action must be an object")
    kind = str(request.get("kind") or "")
    arguments = {name: value for name, value in request.items() if name != "kind"}
    key = _key(task, kind, arguments)
    if key in _audit(task.ticket):
        return None
    if refusal := action_refusal(kind, arguments):
        outcome = ActionOutcome(executed=False, detail=refusal)
    else:
        try:
            outcome = ActionOutcome(executed=True, detail=_PROCESSORS[CoordinatorActionKind(kind)](task, arguments))
        except ActionRefusedError as exc:
            outcome = ActionOutcome(executed=False, detail=str(exc))
    task.ticket.merge_extra(
        merge_into_dicts={AUDIT_KEY: {key: {"kind": kind, "executed": outcome.executed, "detail": outcome.detail}}}
    )
    if not outcome.executed:
        logger.warning("Coordinator action %s refused for task %s: %s", kind or "(none)", task.pk, outcome.detail)
    return outcome


def _audit(ticket: Ticket) -> Mapping[str, object]:
    ticket.refresh_from_db(fields=["extra"])
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    audit = extra.get(AUDIT_KEY)
    return audit if isinstance(audit, dict) else {}


def run_coordinator_actions(task: Task, requests: object) -> list[str]:
    """Owner-visible outcomes for each proposal, including refusals and partial failure."""
    if not isinstance(requests, list):
        return []
    receipts: list[str] = []
    for request in requests:
        try:
            outcome = _run_one(task, request)
        except Exception:
            logger.exception("Coordinator action failed for task %s", task.pk)
            kind = str(request.get("kind") or "action") if isinstance(request, dict) else "action"
            receipts.append(f"Could not confirm {kind}; earlier confirmed actions are listed above.")
            break
        if outcome is None:
            continue
        if outcome.executed:
            receipts.append(outcome.detail)
        else:
            kind = str(request.get("kind") or "action") if isinstance(request, dict) else "action"
            receipts.append(f"Could not run {kind}: {outcome.detail}.")
    return receipts
