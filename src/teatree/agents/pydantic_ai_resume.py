"""Durable ``pydantic_ai`` conversation persistence for cached-resume parity (#2886).

The ``claude_sdk`` harness resumes a parked headless run cheaply via the SDK's
own ``--resume <session_id>`` (server-side session storage, see
:func:`teatree.agents._headless_options._get_resume_session_id`). The
``pydantic_ai`` transport has no equivalent server-side session, so its
in-memory conversation (``list[ModelMessage]``) must be persisted by teatree
itself on PARK and rehydrated on RESUME — the piece epic #2565-C names as the
"one new piece" cached-resume needs (BLUEPRINT.md § Loop Topology).

No migration: reuses ``Ticket.extra`` (an already-migrated per-ticket JSON
store — precedent: ``more_prs_coming``, ``prs``) under the
``pydantic_ai_threads`` key, keyed by the PARKED ``Task``'s own pk — the SAME
identifier :func:`~teatree.agents._headless_options._get_resume_session_id`
walks the ``parent_task`` chain to find, so a pydantic_ai resume locates the
same ancestor a claude_sdk resume would. Entries are single-use: a resume
POPS its entry, mirroring ``schedule_headless_resume``'s idempotent chaining
— the store never accumulates stale threads across repeated park/resume
cycles. Follows the same unlocked-outer-read + ``merge_extra``-locked-write
shape ``backends/gitlab/sync_terminal.py`` already uses for the nested
``prs`` dict — one ticket rarely parks two pydantic_ai tasks in the same
instant, so the narrow TOCTOU window that pattern accepts is unchanged here.

Prompt-cache fallback policy (#2886): resending the rehydrated history is the
WHOLE mechanism — no manual ``cache_control`` markers are sent (prompt-cache
semantics differ per provider behind OrcaRouter's OpenAI-compatible surface,
and are opaque to teatree). When the provider recognizes the resent prefix it
reports non-zero ``cache_read_tokens`` (logged on the resuming ``TaskAttempt``
— the same columns the claude_sdk lane already populates); when it does not,
the full context is simply re-paid as ordinary input tokens and logged as
such. Either way the resume NEVER refuses — a cache miss is a cost, not an
error. A missing, malformed, or already-consumed thread degrades the same
way: an empty history, never an exception.

Pop-then-restore (souliane/teatree#2916): :func:`rehydrate_thread_for_resume`
still consumes the entry the moment it is READ, not the moment it is
actually driven through a harness — cheaper than plumbing a commit-on-success
callback through the async driver. A caller (:mod:`teatree.agents.headless`)
that refuses the dispatch this seeded BEFORE the harness genuinely opens (an
over-budget ticket, a failed OrcaRouter credential) must restore the popped
entry via :func:`persist_parked_thread`, or a run that never happened
silently and irrecoverably destroys the parked conversation.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError
from pydantic_ai.messages import ModelMessagesTypeAdapter

from teatree.agents.result_schema import AgentResultBlob
from teatree.core.models import Task

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

    from teatree.core.models.types import TicketExtra

logger = logging.getLogger(__name__)

_THREAD_STORE_KEY = "pydantic_ai_threads"


def persist_parked_thread(task: Task, history: "list[ModelMessage]") -> None:
    """Durably store *history* keyed to *task*'s own pk, for a later resume.

    Called once, at PARK time (a ``needs_user_input`` STOP) — never on an
    ordinary completed run, where there is nothing to resume. Also reused to
    RESTORE a thread :func:`rehydrate_thread_for_resume` already popped when
    the dispatch it seeded is refused before it ever runs (see
    :func:`teatree.agents.headless._restore_unconsumed_resume_thread`).
    """
    ticket = task.ticket
    threads = dict(ticket.extra.get(_THREAD_STORE_KEY, {}) if isinstance(ticket.extra, dict) else {})
    threads[str(task.pk)] = ModelMessagesTypeAdapter.dump_python(history, mode="json")
    ticket.merge_extra(set_keys=cast("TicketExtra", {_THREAD_STORE_KEY: threads}))
    logger.info("Persisted pydantic_ai thread for parked task %s (%d messages)", task.pk, len(history))


def maybe_persist_on_park(task: Task, result: AgentResultBlob, thread: "list[ModelMessage] | None") -> None:
    """Persist *thread* iff *result* is a ``needs_user_input`` PARK — else a no-op.

    An ordinary completed run (or one with no ``thread`` — claude_sdk, or a
    watchdog-interrupted run) has nothing to resume.

    Known limitation (souliane/teatree#3605): this persists ONLY on a
    ``needs_user_input`` park, keyed under *task*'s pk, and
    :func:`rehydrate_thread_for_resume` walks ``parent_task`` only. A
    usage-LIMIT-parked run (``usage_window.park_or_rotate_on_limit``) re-queues as
    ITSELF, so it neither persists here nor rehydrates on resume — it resumes with
    a fresh conversation. The fix (persist under the task's own pk on a limit-park +
    rehydrate from ``task`` itself) is tracked separately; a cache miss is a cost,
    never an error.
    """
    if result.get("needs_user_input") and thread:
        persist_parked_thread(task, thread)


@dataclass(frozen=True)
class ResumedThread:
    """A parked ancestor's thread, popped for an in-flight resume attempt.

    *ancestor* travels alongside *history* so a caller that refuses the
    dispatch this seeded BEFORE the harness genuinely opens (an over-budget
    ticket, an OrcaRouter credential failure) can restore the entry via
    :func:`persist_parked_thread` — the pop is meant to be single-use only
    once a run actually consumes the conversation, not merely once it is read
    (souliane/teatree#2916).
    """

    ancestor: Task
    history: "list[ModelMessage]"


def rehydrate_thread_for_resume(task: Task) -> "ResumedThread | None":
    """Reload the nearest parked ancestor's thread, or ``None`` when none parked.

    Walks ``parent_task`` exactly like
    :func:`~teatree.agents._headless_options._get_resume_session_id`, so a
    pydantic_ai resume finds the SAME ancestor a claude_sdk resume would.
    Consumes the entry on read (single-use). Never raises — see the module
    docstring's fallback policy.
    """
    current = task.parent_task
    while current is not None:
        history = _pop_thread(current)
        if history is not None:
            return ResumedThread(ancestor=current, history=history)
        current = current.parent_task
    return None


def _pop_thread(task: Task) -> "list[ModelMessage] | None":
    ticket = task.ticket
    threads = dict(ticket.extra.get(_THREAD_STORE_KEY, {}) if isinstance(ticket.extra, dict) else {})
    raw = threads.pop(str(task.pk), None)
    if raw is None:
        return None
    ticket.merge_extra(set_keys=cast("TicketExtra", {_THREAD_STORE_KEY: threads}))
    try:
        return ModelMessagesTypeAdapter.validate_python(raw)
    except ValidationError:
        logger.warning("Discarding unparsable pydantic_ai thread for task %s", task.pk)
        return []
