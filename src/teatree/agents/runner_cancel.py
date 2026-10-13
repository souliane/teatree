"""Settle a cancelled run's processes before its checkout claim is released."""

from pathlib import Path

from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Task, TaskAttempt
from teatree.core.models.task_cancel_source import cancel_source
from teatree.core.models.task_claim import claim_generation
from teatree.core.worktree.cancel_processes import (
    CheckoutProcess,
    current_pid_namespace,
    stop_owned_writers,
    wait_for_owned_checkout_processes,
)


def settle_cancelled_checkout(task: Task, checkout: Path, *, beat_seconds: float) -> None:
    if not Task.objects.filter(pk=task.pk, status=Task.Status.FAILED, failure_kind=FailureKind.CANCELLED).exists():
        return
    survivors = wait_for_owned_checkout_processes(checkout, beat_seconds=beat_seconds)
    if not survivors:
        return
    if cancel_source(task) == "owner":
        stop_owned_writers(checkout)
    elif cancel_source(task) == "agent":
        candidates = [
            {"pid": process.pid, "start_ticks": process.started}
            for process in survivors
            if isinstance(process, CheckoutProcess)
            and process.descendant
            and process.writer
            and not process.shared_server
        ]
        namespace = current_pid_namespace()
        if candidates and namespace:
            attempt = TaskAttempt.objects.filter(task=task, result__has_key="cancel_source").order_by("-pk").first()
            if attempt is not None:
                attempt.result = dict(attempt.result or {}) | {
                    "surviving_writers": {
                        "namespace": namespace,
                        "claim_generation": claim_generation(task),
                        "processes": candidates,
                    }
                }
                attempt.save(update_fields=["result"])
