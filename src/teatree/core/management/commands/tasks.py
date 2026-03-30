from django_typer.management import TyperCommand, command

from teatree.core.models import Task


class Command(TyperCommand):
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
