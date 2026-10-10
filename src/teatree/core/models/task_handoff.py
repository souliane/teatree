"""Needs-user-input handoff over ``Task`` — park the question, resume on answer.

The model-touching half of the headless ask-loop (souliane/teatree#headless
question routing): when an agent returns ``needs_user_input`` and STOPS,
:func:`park_for_user_input` records the question on the lane that can reach the
user, and :func:`resume_on_answer` re-queues a headless continuation
once the owner's answer lands. Split out of ``task.py`` (which is at its module-health
LOC cap) — the thin ``Task`` call sites delegate here. The functions take a
``Task`` so they stay free of model-class state, mirroring ``task_repair.py``.
"""

import logging

from teatree.core.modelkit.owner_decision import owner_decision
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.errors import NoPlanArtifactError
from teatree.core.models.plan_decision import refuse_unplanned_mint
from teatree.core.models.question_subject import finished_subject_reason
from teatree.core.models.question_text import question_fingerprint
from teatree.core.models.session import Session
from teatree.core.models.task import Task

_DEFAULT_REASON = "Agent needs human input"

logger = logging.getLogger(__name__)

#: What :func:`schedule_resume` STORES: the owner's answer, true on any conversation the retry
#: ends up carrying. The migration that back-filled ``session_continuation`` keys on this prefix.
RESUME_ANSWER_PREFIX = "The user answered your earlier question:"

#: What :func:`dispatch_reason` DERIVES: true only while the dispatch actually resumes. Storing it
#: froze it into the prompt, and a requeue that answered FRESH still told the agent to continue
#: from a decision point its new conversation has never seen.
RESUME_CONTINUATION_CLAUSE = "Continue from where you left off — do NOT restart the task from scratch."


def dispatch_reason(task: Task) -> str:
    """The prompt instruction a dispatch of *task* carries — the stored reason, made true for it."""
    reason = task.execution_reason
    if not reason.startswith(RESUME_ANSWER_PREFIX):
        return reason
    # Rows queued before the clause moved out of storage still carry it inline, so it is taken
    # off first: that both spares them a doubled sentence and applies the FRESH rule to them.
    answered = reason.replace(RESUME_CONTINUATION_CLAUSE, "").strip()
    if task.session_continuation == Task.SessionContinuation.FRESH:
        return answered
    return f"{answered} {RESUME_CONTINUATION_CLAUSE}"


def park_for_user_input(task: Task) -> None:
    """Park a ``needs_user_input`` STOP: an owner question only when it names an owner decision.

    A stop naming no :class:`OwnerDecision` kind is recorded internal and resumes nothing. An owner
    question this ticket already asked resumes the task at once only through :func:`resume_on_answer`.
    """
    row = record_deferred_question(task)
    if row.parked_task_id != task.pk:
        resume_on_answer(row, task)


def resume_on_answer(row: DeferredQuestion, task: Task | None) -> Task | None:
    """Resume *task* with *row*'s answer iff the owner answered an owner question about an open subject."""
    if (
        task is None
        or row.audience != DeferredQuestion.Audience.OWNER_QUESTION
        or not row.answered_on_owner_channel
        or finished_subject_reason(row) is not None
    ):
        return None
    try:
        return schedule_resume(task, answer=row.answer_text)
    except NoPlanArtifactError:
        logger.warning("Answer to question %s kept; task %s was not resumed", row.pk, task.pk)
        return None


def record_deferred_question(task: Task) -> DeferredQuestion:
    """Record a mirror-pending DeferredQuestion correlated to *task*.

    ``slack_ts``/``slack_channel`` stay empty so the tick-level poster scanner can mirror
    an owner row; ``run_id`` carries the resumable agent session and ``parked_task`` is the
    correlation a reply walks back to re-queue a headless resume. The audience comes from
    the envelope's ``user_input_kind``: without an owner decision the row is internal. An
    owner question is keyed per ticket, so its answer is never asked for twice there.
    """
    last = task.attempts.order_by("-pk").first()
    result = last.result if last else {}
    reason = str(result.get("user_input_reason", _DEFAULT_REASON)) if last else "Agent needs input"
    decision = owner_decision(result.get("user_input_kind"))
    scope = f"{task.ticket.pk}:" if decision is not None else ""
    return DeferredQuestion.record(
        reason,
        task_session=task.session,
        run_id=last.agent_session_id if last else "",
        dedupe_marker=f"needs-input:{scope}{question_fingerprint(reason)}",
        parked_task=task,
        decision=decision,
        checked=result.get("user_input_checked", ()),
    )


def schedule_resume(task: Task, *, answer: str) -> Task:
    """Re-queue a HEADLESS followup that resumes *task* with *answer*.

    Closes the headless ask-loop: the agent emitted ``needs_user_input`` and
    STOPPED, the question reached the user, and the reply now resumes the run.
    The followup is typed ``SessionContinuation.PARENT``, so ``resume_session_id``
    reads this task's captured SDK session — the agent CONTINUES from the
    decision point, it does not restart from scratch. The answer is prepended to
    the work prompt via ``execution_reason``; :func:`dispatch_reason` adds the
    continue-where-you-left-off instruction for as long as the retry really does.
    Idempotent: a resume already queued for this task is returned, never duplicated. An
    implementing phase on a ticket with no plan decision raises ``NoPlanArtifactError``.
    """
    existing = task.child_tasks.filter(  # ty: ignore[unresolved-attribute]
        status__in=[Task.Status.PENDING, Task.Status.CLAIMED],
    ).first()
    if existing is not None:
        return existing
    refuse_unplanned_mint(task.ticket, phase=task.phase)
    last = task.attempts.order_by("-pk").first()
    agent_session_id = last.agent_session_id if last else ""
    session = Session.objects.create(ticket=task.ticket, agent_id=agent_session_id or "headless-resume")
    reason = f"{RESUME_ANSWER_PREFIX} {answer}."
    return Task.objects.create(
        ticket=task.ticket,
        session=session,
        phase=task.phase,
        execution_reason=reason,
        parent_task=task,
        session_continuation=Task.SessionContinuation.PARENT,
    )
