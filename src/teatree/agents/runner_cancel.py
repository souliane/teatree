"""Settle a cancelled run's processes before its checkout claim is released."""

from pathlib import Path

from teatree.agents.cancel_processes import stop_owned_writers, wait_for_owned_checkout_processes
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Task
from teatree.core.models.task_cancel_source import cancel_source


def settle_cancelled_checkout(task: Task, checkout: Path, *, beat_seconds: float) -> None:
    if not Task.objects.filter(pk=task.pk, status=Task.Status.FAILED, failure_kind=FailureKind.CANCELLED).exists():
        return
    survivors = wait_for_owned_checkout_processes(checkout, beat_seconds=beat_seconds)
    if not survivors:
        return
    if cancel_source(task) == "owner":
        stop_owned_writers(checkout)
