"""Durable ``pydantic_ai`` conversation persistence for cached-resume parity (#2886).

The ``claude_sdk`` harness resumes a parked headless run cheaply via the SDK's
own ``--resume <session_id>`` (server-side session storage, see
:func:`teatree.agents.session_lineage.resume_session_id`). The
``pydantic_ai`` transport has no equivalent server-side session, so its
in-memory conversation (``list[ModelMessage]``) must be persisted by teatree
itself and rehydrated on RESUME — the piece epic #2565-C names as the
"one new piece" cached-resume needs (BLUEPRINT.md § Loop Topology).

Lifecycle: every run that reaches a recorded outcome RETAINS its conversation under its
own task pk before the outcome is written (:func:`retain_run_thread`), so a failure, a
limit park and a needs-input park all leave something the next dispatch can continue —
and a concurrent requeue can never observe the failed row before its thread exists. The
entry is DISCARDED once nothing will continue it: the run completed without asking for
input (:func:`release_finished_thread`), or the requeue sweep decided the row will not retry.

No migration: reuses ``Ticket.extra`` (an already-migrated per-ticket JSON
store — precedent: ``more_prs_coming``, ``prs``) under the
``pydantic_ai_threads`` key, keyed by the ``Task``'s own pk — the SAME
identifier :func:`~teatree.agents.session_lineage.resume_session_id` reads off the
typed continuation lineage, so a pydantic_ai resume locates the same source a
claude_sdk resume would. Entries are single-use: a resume POPS its entry. An add
merges into the LOCKED re-read of ``extra``, because parallel children of one ticket
retain their threads concurrently; a removal re-reads ``extra`` first, so a removal
from a long-held ticket instance cannot drop a sibling's entry written meanwhile.

Prompt-cache fallback policy (#2886): resending the rehydrated history is the
WHOLE mechanism — no manual ``cache_control`` markers are sent (prompt-cache
semantics differ per provider behind the OpenAI-compatible surface,
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
callback through the async driver. A caller (:mod:`teatree.agents.runner`)
that refuses the dispatch this seeded BEFORE the harness genuinely opens (an
over-budget ticket, a failed backend credential) must restore the popped
entry via :func:`persist_parked_thread`, or a run that never happened
silently and irrecoverably destroys the parked conversation.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError
from pydantic_ai.messages import ModelMessagesTypeAdapter

from teatree.agents.session_lineage import resumable_lineage
from teatree.core.models import Task
from teatree.core.models.ticket_evidence import TASK_THREADS_KEY

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage


logger = logging.getLogger(__name__)


def persist_parked_thread(task: Task, history: "list[ModelMessage]") -> None:
    """Durably store *history* keyed to *task*'s own pk, for a later resume.

    Also reused to RESTORE a thread :func:`rehydrate_thread_for_resume` already popped
    when the dispatch it seeded is refused before it ever runs (see
    :func:`teatree.agents.runner._restore_unconsumed_resume_thread`).
    """
    dumped = ModelMessagesTypeAdapter.dump_python(history, mode="json")
    task.ticket.merge_extra(merge_into_dicts={TASK_THREADS_KEY: {str(task.pk): dumped}})
    logger.info("Persisted pydantic_ai thread for task %s (%d messages)", task.pk, len(history))


def retain_run_thread(task: Task, thread: "list[ModelMessage] | None") -> None:
    """Keep a finished run's conversation under *task*'s own pk; a transport with none keeps nothing."""
    if thread:
        persist_parked_thread(task, thread)


def release_finished_thread(task: Task) -> None:
    """Drop *task*'s conversation once it completed without asking for input — nothing will continue it."""
    last_attempt = task.attempts.order_by("-pk").first()  # ty: ignore[unresolved-attribute]
    if last_attempt is not None and (last_attempt.result or {}).get("needs_user_input"):
        return
    if Task.objects.filter(pk=task.pk, status=Task.Status.COMPLETED).exists():
        task.ticket.pop_task_thread(int(task.pk))


@dataclass(frozen=True)
class ResumedThread:
    """A parked ancestor's thread, popped for an in-flight resume attempt.

    *ancestor* travels alongside *history* so a caller that refuses the
    dispatch this seeded BEFORE the harness genuinely opens (an over-budget
    ticket, a backend credential failure) can restore the entry via
    :func:`persist_parked_thread` — the pop is meant to be single-use only
    once a run actually consumes the conversation, not merely once it is read
    (souliane/teatree#2916).
    """

    ancestor: Task
    history: "list[ModelMessage]"


def rehydrate_thread_for_resume(task: Task) -> "ResumedThread | None":
    """Reload the parked thread of the conversation *task* is typed to continue.

    Walks :func:`~teatree.agents.session_lineage.resumable_lineage` exactly like
    :func:`~teatree.agents.session_lineage.resume_session_id`, so a pydantic_ai resume
    finds the SAME source a claude_sdk resume would. A usage-limit park re-queues the
    same row typed SELF, which is how its own-pk entry is reached (#3605). Consumes the
    entry on read (single-use). Never raises — see the module docstring's fallback policy.
    """
    for current in resumable_lineage(task):
        history = _pop_thread(current)
        if history is not None:
            return ResumedThread(ancestor=current, history=history)
    return None


def _pop_thread(task: Task) -> "list[ModelMessage] | None":
    raw = task.ticket.pop_task_thread(int(task.pk))
    if raw is None:
        return None
    try:
        return ModelMessagesTypeAdapter.validate_python(raw)
    except ValidationError:
        logger.warning("Discarding unparsable pydantic_ai thread for task %s", task.pk)
        return []
