"""The halt question older code recorded, kept so the drains that retire stored rows stay tested."""

from collections.abc import Generator
from contextlib import contextmanager
from unittest.mock import patch

from django.db import transaction

from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import Task
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.transient_requeue import _stamp_halt, escalation_marker


def record_legacy_halt(task: Task, *, reason: str) -> None:
    where = task.ticket.issue_url or f"ticket {task.ticket.pk}"
    phase = normalize_phase(task.phase)
    question = (
        f"[repair-halt ticket={task.ticket.pk} phase={phase!r}] Auto-retry halted on {where}: {reason} "
        "Re-queueing is stopped so it does not retry a doomed failure forever. "
        "How should it proceed — investigate, rework, or ignore?"
    )
    with transaction.atomic():
        _stamp_halt(task)
        task.ticket.pop_task_thread(int(task.pk))
        DeferredQuestion.record(question, task_session=task.session, dedupe_marker=escalation_marker(task))


@contextmanager
def halts_record_legacy_questions() -> Generator[None]:
    with patch("teatree.loop.transient_requeue._escalate_once", side_effect=record_legacy_halt):
        yield
