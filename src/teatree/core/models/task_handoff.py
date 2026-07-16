"""Needs-user-input handoff over ``Task`` — park the question, resume on answer.

The model-touching half of the headless ask-loop (souliane/teatree#headless
question routing): when an agent returns ``needs_user_input`` and STOPS,
:func:`park_for_user_input` records the question on the lane that can reach the
user, and :func:`schedule_headless_resume` re-queues a headless continuation
once the answer lands. Split out of ``task.py`` (which is at its module-health
LOC cap) — the thin ``Task`` call sites delegate here. The functions take a
``Task`` so they stay free of model-class state, mirroring ``task_repair.py``.
"""

from teatree.config import AgentRuntime, get_effective_settings
from teatree.core.models.deferred_question import DeferredQuestion, question_fingerprint
from teatree.core.models.session import Session
from teatree.core.models.task import Task

_DEFAULT_REASON = "Agent needs human input"


def park_for_user_input(task: Task) -> None:
    """Park a ``needs_user_input`` STOP on the lane that can reach the user.

    Interactive lane (``agent_runtime=interactive``): a human is at the harness,
    so schedule an in-session interactive followup carrying the question.
    Headless lane (any SDK runtime): there is no terminal, so record a durable,
    mirror-pending :class:`DeferredQuestion` correlated to this task; the
    tick-level poster scanner posts it to Slack and the reply re-queues a
    headless resume from the captured session.
    """
    if get_effective_settings().agent_runtime is AgentRuntime.INTERACTIVE:
        schedule_interactive_followup(task)
    else:
        record_deferred_question(task)


def schedule_interactive_followup(task: Task) -> Task:
    """Create a new interactive task for human handoff, carrying the headless session_id."""
    last = task.attempts.order_by("-pk").first()  # ty: ignore[unresolved-attribute]
    reason = str(last.result.get("user_input_reason", _DEFAULT_REASON)) if last else "Agent needs input"
    agent_session_id = last.agent_session_id if last else ""
    session = Session.objects.create(ticket=task.ticket, agent_id=agent_session_id or "interactive-followup")
    return Task.objects.create(
        ticket=task.ticket,
        session=session,
        phase=task.phase,
        execution_target=Task.ExecutionTarget.INTERACTIVE,
        execution_reason=reason,
        parent_task=task,
    )


def record_deferred_question(task: Task) -> DeferredQuestion:
    """Record a mirror-pending DeferredQuestion correlated to *task*.

    The headless-lane STOP record: ``slack_ts``/``slack_channel`` are left empty
    (un-mirrored) so the tick-level poster scanner posts it to the user's Slack
    DM and stamps the mirror coordinates. ``run_id`` carries the resumable agent
    session for traceability; ``parked_task`` is the canonical correlation the
    reply scanner walks back to re-queue a headless resume.
    """
    last = task.attempts.order_by("-pk").first()  # ty: ignore[unresolved-attribute]
    reason = str(last.result.get("user_input_reason", _DEFAULT_REASON)) if last else "Agent needs input"
    agent_session_id = last.agent_session_id if last else ""
    # Collapse identical needs-input reasons (e.g. the eight "I lack tools" review
    # failures) to one queued question via a normalized-text fingerprint.
    return DeferredQuestion.record(
        reason,
        session_id=str(task.session_id or ""),  # ty: ignore[unresolved-attribute]
        run_id=agent_session_id or "",
        dedupe_marker=f"needs-input:{question_fingerprint(reason)}",
        parked_task=task,
    )


def schedule_headless_resume(task: Task, *, answer: str) -> Task:
    """Re-queue a HEADLESS followup that resumes *task* with *answer*.

    Closes the headless ask-loop: the agent emitted ``needs_user_input`` and
    STOPPED, the question reached the user, and the reply now resumes the run.
    The followup chains ``parent_task=task`` so ``_get_resume_session_id`` walks
    back to this task's captured SDK session — the agent CONTINUES from the
    decision point, it does not restart from scratch. The answer is prepended to
    the work prompt via ``execution_reason``. Idempotent: a resume already queued
    for this task is returned, never duplicated.
    """
    existing = task.child_tasks.filter(  # ty: ignore[unresolved-attribute]
        execution_target=Task.ExecutionTarget.HEADLESS,
        status__in=[Task.Status.PENDING, Task.Status.CLAIMED],
    ).first()
    if existing is not None:
        return existing
    last = task.attempts.order_by("-pk").first()  # ty: ignore[unresolved-attribute]
    agent_session_id = last.agent_session_id if last else ""
    session = Session.objects.create(ticket=task.ticket, agent_id=agent_session_id or "headless-resume")
    reason = (
        f"The user answered your earlier question: {answer}. "
        "Continue from where you left off — do NOT restart the task from scratch."
    )
    return Task.objects.create(
        ticket=task.ticket,
        session=session,
        phase=task.phase,
        execution_target=Task.ExecutionTarget.HEADLESS,
        execution_reason=reason,
        parent_task=task,
    )
