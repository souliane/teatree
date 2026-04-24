import os
import pathlib
from typing import IO, Annotated, TypedDict, cast

import typer
from django_typer.management import TyperCommand, command
from rich.console import Console
from rich.table import Table

from teatree.core.models import InvalidTransitionError, Task, TaskAttempt


class TaskRow(TypedDict):
    task_id: int
    ticket_id: int
    status: str
    execution_target: str
    phase: str
    execution_reason: str
    claimed_by: str


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
    ) -> list[TaskRow]:
        qs = Task.objects.all().order_by("pk")
        if status:
            qs = qs.filter(status=status)
        if execution_target:
            qs = qs.filter(execution_target=execution_target)
        rows: list[TaskRow] = [
            TaskRow(
                task_id=task.pk,
                ticket_id=task.ticket_id,
                status=task.status,
                execution_target=task.execution_target,
                phase=task.phase,
                execution_reason=task.execution_reason,
                claimed_by=task.claimed_by,
            )
            for task in qs
        ]
        _render_tasks_table(rows, stream=cast("IO[str]", self.stdout))
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

    @command()
    def start(
        self,
        task_id: Annotated[int, typer.Argument(help="Task ID; omit to start the next pending interactive task.")] = 0,
        claimed_by: Annotated[str, typer.Option(help="Worker identifier stored on the claim.")] = "cli",
    ) -> None:
        """Claim an interactive task and exec ``claude`` in the current terminal."""
        task = self._resolve_interactive_task(task_id=task_id, claimed_by=claimed_by)
        if task is None:
            self.stdout.write("No interactive tasks pending.")
            return

        command_argv = _build_claude_command(task)
        TaskAttempt.objects.create(task=task, execution_target=task.execution_target, launch_url="")

        self.stdout.write(f"Starting task {task.pk} (ticket {task.ticket.ticket_number}) in the current terminal…")
        _exec_inline(command_argv)

    def _resolve_interactive_task(self, *, task_id: int, claimed_by: str) -> Task | None:
        if task_id:
            try:
                task = Task.objects.get(pk=task_id)
            except Task.DoesNotExist:
                self.stderr.write(f"Task {task_id} not found.")
                raise SystemExit(1) from None

            if task.execution_target != Task.ExecutionTarget.INTERACTIVE:
                self.stderr.write(f"Task {task_id} is not an interactive task.")
                raise SystemExit(1)

            try:
                task.claim(claimed_by=claimed_by)
            except InvalidTransitionError as exc:
                self.stderr.write(f"Cannot claim task {task_id}: {exc}")
                raise SystemExit(1) from None
            return task

        return self._claim_next_task(execution_target=Task.ExecutionTarget.INTERACTIVE, claimed_by=claimed_by)

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
        from teatree.agents.headless import run_headless  # noqa: PLC0415
        from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

        attempt = run_headless(
            task,
            phase=task.phase,
            overlay_skill_metadata=get_overlay().metadata.get_skill_metadata(),
        )
        return {"exit_code": str(attempt.exit_code), "attempt_id": str(attempt.pk)}

    @staticmethod
    def _execute_runtime(task: Task) -> dict[str, str]:
        from teatree.agents.web_terminal import launch_web_session  # noqa: PLC0415
        from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

        attempt = launch_web_session(
            task,
            overlay_skill_metadata=get_overlay().metadata.get_skill_metadata(),
        )
        return {"launch_url": attempt.launch_url, "attempt_id": str(attempt.pk)}


_STATUS_STYLES: dict[str, str] = {
    "pending": "yellow",
    "claimed": "cyan",
    "completed": "green",
    "failed": "red",
}


def _render_tasks_table(rows: list[TaskRow], *, stream: IO[str] | None = None) -> None:
    console = Console(file=stream) if stream is not None else Console()
    if not rows:
        console.print("[dim]No tasks.[/dim]")
        return

    table = Table(title=f"Tasks ({len(rows)})", show_lines=False)
    table.add_column("ID", justify="right", style="bold")
    table.add_column("Ticket", justify="right")
    table.add_column("Status")
    table.add_column("Target")
    table.add_column("Phase")
    table.add_column("Claimed by")
    table.add_column("Reason", overflow="fold", max_width=60)

    for row in rows:
        status = row["status"]
        style = _STATUS_STYLES.get(status, "")
        table.add_row(
            str(row["task_id"]),
            str(row["ticket_id"]),
            f"[{style}]{status}[/]" if style else status,
            row["execution_target"],
            row["phase"] or "-",
            row["claimed_by"] or "-",
            row["execution_reason"] or "-",
        )

    console.print(table)


def _build_claude_command(task: Task) -> list[str]:
    from teatree.agents.web_terminal import build_interactive_command  # noqa: PLC0415
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

    return build_interactive_command(
        task,
        overlay_skill_metadata=get_overlay().metadata.get_skill_metadata(),
    )


def _exec_inline(argv: list[str]) -> None:
    from teatree.utils.run import run_streamed  # noqa: PLC0415

    orig_cwd = os.environ.get("T3_ORIG_CWD", "")
    cwd = orig_cwd if orig_cwd and pathlib.Path(orig_cwd).is_dir() else None
    rc = run_streamed(argv, cwd=cwd, check=False)
    raise SystemExit(rc)
