"""Scan the ``Task`` table for rows in pending state.

Pending tasks are dispatched to the headless executor (BLUEPRINT § 5.2),
which routes each one to the appropriate phase agent.

The Django ``Task`` model is resolved lazily through ``apps.get_model``
so this module stays importable before ``django.setup()`` runs — the
CLI imports the loop subapp at startup, which transitively pulls this
module in before Django is ready.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from django.apps import apps

from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.core.models.task import Task


@dataclass(slots=True)
class PendingTasksScanner:
    """Yield one ``pending_task`` signal per pending row.

    ``limit`` caps how many rows the scanner emits per tick — large
    backlogs spread across multiple ticks rather than flooding one
    statusline render.
    """

    limit: int = 50
    name: str = "pending_tasks"

    def scan(self) -> list[ScanSignal]:
        task_model = cast("type[Task]", apps.get_model("core", "Task"))
        pending = task_model.objects.filter(status=task_model.Status.PENDING).order_by("id")[: self.limit]
        return [
            ScanSignal(
                kind="pending_task",
                summary=f"Task {task.id} ({task.phase}) pending",
                payload={"task_id": task.id, "phase": task.phase, "ticket_id": task.ticket_id},
            )
            for task in pending
        ]
