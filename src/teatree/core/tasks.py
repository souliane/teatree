from django.tasks import task

from teatree.core.models import Task, Ticket


@task()
def execute_headless_task(task_id: int, phase: str) -> dict[str, object]:
    import traceback  # noqa: PLC0415

    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

    task_obj = Task.objects.get(pk=task_id)
    # Claim here (when the worker actually starts) instead of at enqueue time
    if task_obj.status == Task.Status.PENDING:
        task_obj.claim(claimed_by="headless-worker")
    try:
        from teatree.agents.headless import run_headless  # noqa: PLC0415

        overlay = get_overlay()
        attempt = run_headless(
            task_obj,
            phase=phase,
            overlay_skill_metadata=overlay.metadata.get_skill_metadata(),
        )
    except Exception:
        task_obj.complete_with_attempt(exit_code=1, error=traceback.format_exc())
        raise
    else:
        return {"attempt_id": attempt.pk, "exit_code": attempt.exit_code, "result": attempt.result}


@task()
def drain_headless_queue() -> dict[str, list[int]]:
    """Auto-enqueue pending headless tasks for execution."""
    pending = Task.objects.filter(
        execution_target=Task.ExecutionTarget.HEADLESS,
        status=Task.Status.PENDING,
    ).values_list("pk", "phase")
    enqueued: list[int] = []
    for task_id, phase in pending:
        execute_headless_task.enqueue(task_id, phase)
        enqueued.append(task_id)
    return {"enqueued": enqueued}


@task()
def sync_followup() -> dict[str, int | list[str]]:
    from teatree.core.sync import sync_followup as _sync  # noqa: PLC0415

    result = _sync()
    return {
        "mrs_found": result.mrs_found,
        "tickets_created": result.tickets_created,
        "tickets_updated": result.tickets_updated,
        "errors": result.errors,
    }


@task()
def refresh_followup_snapshot() -> dict[str, int]:
    return {
        "tickets": Ticket.objects.count(),
        "tasks": Task.objects.count(),
        "open_tasks": Task.objects.exclude(status=Task.Status.COMPLETED).count(),
    }
