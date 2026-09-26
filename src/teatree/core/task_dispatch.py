"""Phase-aware queue routing for headless task execution."""

from teatree.core.modelkit.phases import PhaseCost, phase_cost
from teatree.core.tasks import execute_task

CHEAP_TASK_QUEUE = "cheap"


def enqueue_execution(task_id: int, phase: str) -> None:
    """Route draining phases to their own executor; preserve one task body and claim CAS."""
    job = execute_task.using(queue_name=CHEAP_TASK_QUEUE) if phase_cost(phase) is PhaseCost.CHEAP else execute_task
    job.enqueue(task_id, phase)
