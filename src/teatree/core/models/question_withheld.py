"""A withheld owner question hands the task waiting on it to the card that asks it again (#4990).

Lives beside :class:`DeferredQuestion` because ``DeferredQuestion.record`` is what carries the task
across, and a model may not import ``teatree.core``; ``core.question_heal`` does the withholding.
"""

from typing import TYPE_CHECKING

from django.db.models import QuerySet

from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit

if TYPE_CHECKING:
    from teatree.core.models.session import Session
    from teatree.core.models.task import Task

WITHHELD_ACTION = "withheld"


def carried_wait(
    marked: QuerySet[DeferredQuestion], parked_task: "Task | None", task_session: "Session | None"
) -> "tuple[Task | None, Session | None]":
    """The task and session a new card under *marked* inherits: the caller's, else a pending withheld row's."""
    if parked_task is not None:
        return parked_task, task_session
    withheld = marked.filter(
        pk__in=DeferredQuestionAudit.objects.filter(action=WITHHELD_ACTION).values("question_id"),
        audience=DeferredQuestion.Audience.INTERNAL,
        answered_at__isnull=True,
        dismissed_at__isnull=True,
    ).first()
    return (withheld.parked_task, task_session or withheld.task_session) if withheld else (None, task_session)
