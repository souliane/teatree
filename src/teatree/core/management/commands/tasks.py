from typing import Annotated

import typer
from django_typer.management import TyperCommand, command

from teatree.core.models import Task


class Command(TyperCommand):
    @command()
    def cancel(self, task_id: int, *, confirm: bool = False) -> None:
        from django.db import transaction  # noqa: PLC0415

        with transaction.atomic():
            try:
                task = Task.objects.select_for_update().get(pk=task_id)
            except Task.DoesNotExist:
                self.stderr.write(f"Task {task_id} not found.")
                raise SystemExit(1) from None

            if task.status == Task.Status.CLAIMED and not confirm:
                self.stderr.write(f"Task {task_id} is currently claimed. Pass --confirm to cancel it.")
                raise SystemExit(1)

            if task.status in {Task.Status.COMPLETED, Task.Status.FAILED}:
                self.stderr.write(f"Task {task_id} already finished ({task.status}).")
                raise SystemExit(1)

            task.fail()
        self.stdout.write(f"Task {task_id} cancelled.")

    @command(name="list")
    def list_tasks(
        self,
        status: Annotated[str | None, typer.Option(help="Filter by status")] = None,
        execution_target: Annotated[str | None, typer.Option(help="Filter by execution target")] = None,
    ) -> list[dict[str, object]]:
        qs = Task.objects.all().order_by("pk")
        if status:
            qs = qs.filter(status=status)
        if execution_target:
            qs = qs.filter(execution_target=execution_target)
        rows: list[dict[str, object]] = [
            {
                "task_id": task.pk,
                "ticket_id": task.ticket_id,
                "status": task.status,
                "execution_target": task.execution_target,
                "phase": task.phase,
                "execution_reason": task.execution_reason,
                "claimed_by": task.claimed_by,
            }
            for task in qs
        ]
        for row in rows:
            self.stdout.write(str(row))
        return rows

    @command()
    def claim(self, execution_target: str = "headless", claimed_by: str = "worker") -> int | None:
        task = self._claim_next_task(execution_target=execution_target, claimed_by=claimed_by)
        return int(task.pk) if task else None

    @command()
    def work_next_sdk(self, claimed_by: str = "worker") -> dict[str, str] | None:
        task = self._claim_next_task(execution_target=Task.ExecutionTarget.HEADLESS, claimed_by=claimed_by)
        if task is None:
            return None
        return self._execute_sdk(task)

    @command()
    def work_next_user_input(self, claimed_by: str = "worker") -> dict[str, str] | None:
        task = self._claim_next_task(execution_target=Task.ExecutionTarget.INTERACTIVE, claimed_by=claimed_by)
        if task is None:
            return None
        return self._execute_runtime(task)

    def _claim_next_task(self, *, execution_target: str, claimed_by: str) -> Task | None:
        if execution_target == Task.ExecutionTarget.INTERACTIVE:
            queryset = Task.objects.claimable_for_interactive()
        else:
            queryset = Task.objects.claimable_for_headless()

        task = queryset.first()
        if task is None:
            return None

        task.claim(claimed_by=claimed_by)
        return task

    @staticmethod
    def _execute_sdk(task: Task) -> dict[str, str]:
        from teatree.agents.sdk import run_headless_task  # noqa: PLC0415
        from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

        result = run_headless_task(
            task,
            phase=task.phase,
            overlay_skill_metadata=get_overlay().metadata.get_skill_metadata(),
        )
        return {"runtime": result.runtime, "artifact_path": result.artifact_path}

    @staticmethod
    def _execute_runtime(task: Task) -> dict[str, str]:
        from teatree.agents.terminal import run_interactive_task  # noqa: PLC0415
        from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

        result = run_interactive_task(
            task,
            phase=task.phase,
            overlay_skill_metadata=get_overlay().metadata.get_skill_metadata(),
        )
        return {"runtime": result.runtime, "artifact_path": result.artifact_path}
