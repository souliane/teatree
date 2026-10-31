"""Post a conversation's planning result once into its bound Slack thread.

A conversation row is bookkeeping, so completing its planning task advances no ticket and
nothing else tells the requester the plan exists. The text is built here from the returned
plan alone: never the thread, never the ticket's description.
"""

import logging
import re

from teatree.agents.result_schema import AgentResultBlob
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import Task
from teatree.core.notify import NotifyKind, NotifyOptions, notify_user_outcome

logger = logging.getLogger(__name__)

SUMMARY_LIMIT = 700
BUILD_HINT = "To have this built, reply in this thread and say so; I will file it as a tracked ticket."


def post_conversation_plan(task: Task, result: AgentResultBlob, *, phase: str) -> None:
    ticket = task.ticket
    plan_text = result.get("plan_text")
    if (
        normalize_phase(phase or task.phase) != "planning"
        or not ticket.is_conversation()
        or not isinstance(plan_text, str)
        or not plan_text.strip()
    ):
        return
    binding = ticket.slack_threads.first()
    if binding is None:
        return
    try:
        notify_user_outcome(
            f"{plan_summary(plan_text)}\n{BUILD_HINT}",
            kind=NotifyKind.ANSWER,
            idempotency_key=f"conversation-plan:{task.pk}",
            audience=NotifyAudience.OWNER_DELIVERY,
            options=NotifyOptions(requested_push=True, ticket_id=ticket.pk, thread_ts=binding.thread_ts),
        )
    except Exception:
        logger.warning("conversation plan post failed for task %s", task.pk, exc_info=True)


def plan_summary(plan_text: str) -> str:
    first, *rest = re.split(r"\n\s*\n", plan_text.strip())
    if first.startswith("#") and rest:
        first = f"{first}\n{rest[0]}"
    summary = re.sub(r"^#+\s*", "", first)
    return summary if len(summary) <= SUMMARY_LIMIT else f"{summary[:SUMMARY_LIMIT].rstrip()}…"
