"""Phase-aware queue routing and the one admission walk for headless task execution."""

import logging

from teatree.core import agent_admission
from teatree.core.admission.dispatch_mask import headless_admission_block_reason
from teatree.core.admission_priority import ADMISSION_ORDER, admission_priority_annotations
from teatree.core.modelkit.phases import PhaseCost, phase_cost
from teatree.core.models import Task
from teatree.core.tasks import execute_task

logger = logging.getLogger(__name__)

CHEAP_TASK_QUEUE = "cheap"


def enqueue_execution(task_id: int, phase: str) -> None:
    """Route draining phases to their own executor; preserve one task body and claim CAS."""
    job = execute_task.using(queue_name=CHEAP_TASK_QUEUE) if phase_cost(phase) is PhaseCost.CHEAP else execute_task
    job.enqueue(task_id, phase)


def admit_waiting_tasks(*, at: str) -> list[int]:
    """Seat and enqueue waiting rows in admission order; return the enqueued pks.

    The post_save receiver and the queue drain both run this walk, so a newly saved row
    can never be admitted ahead of an older row waiting in the same lane.
    """
    if blocked := headless_admission_block_reason():
        logger.info("Withholding headless admission at %s: %s", at, blocked)
        return []
    admission = agent_admission.agent_admission_verdict()
    admission.log_denials()
    waiting = (
        Task.objects.waiting_for_admission().annotate(**admission_priority_annotations()).order_by(*ADMISSION_ORDER)
    )
    enqueued: list[int] = []
    for task in waiting:
        if task.ticket.has_dispatchable_overlay() and admission.admit(task.pk, task.phase, at=at):
            enqueue_execution(task.pk, task.phase)
            logger.info("Enqueued task %s (phase=%s) at %s", task.pk, task.phase, at)
            enqueued.append(task.pk)
    return enqueued
