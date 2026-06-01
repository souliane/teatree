import logging
import os
import pathlib
import shutil
import sys
from typing import IO, Annotated, TypedDict, cast

import typer
from django_typer.management import TyperCommand, command
from rich.console import Console
from rich.table import Table

from teatree.agents.headless import UUID_RE
from teatree.agents.prompt import build_interactive_context
from teatree.agents.skill_bundle import resolve_skill_bundle
from teatree.core.models import InvalidTransitionError, Task, TaskAttempt, Ticket
from teatree.core.overlay_loader import get_overlay

logger = logging.getLogger(__name__)


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
    def create(
        self,
        ticket: Annotated[int, typer.Argument(help="Ticket PK (see `ticket_id` in `tasks list`).")],
        *,
        phase: Annotated[
            str,
            typer.Option(help="Phase: scoping, coding, testing, reviewing, shipping."),
        ] = "",
        reason: Annotated[
            str,
            typer.Option(help="Prompt body for the worker. Use '-' to read from stdin. Overrides --reason-file."),
        ] = "",
        reason_file: Annotated[
            pathlib.Path | None,
            typer.Option(help="Read the prompt body from a file."),
        ] = None,
        interactive: Annotated[
            bool,
            typer.Option(help="Create an interactive task instead of the default headless one."),
        ] = False,
    ) -> dict[str, int | str]:
        """Enqueue the next-phase task for a ticket.

        Used by `/t3:next` to hand off from one phase to the next. Headless by default so a worker
        claims it immediately; pass `--interactive` for tasks that require human input.
        """
        if not phase.strip():
            self.stderr.write("--phase is required (scoping, coding, testing, reviewing, or shipping).")
            raise SystemExit(1)
        body = _resolve_reason(reason=reason, reason_file=reason_file)
        if not body.strip():
            self.stderr.write(
                "--reason (or --reason-file, or stdin via '--reason -') is required and must not be blank."
            )
            raise SystemExit(1)

        try:
            ticket_obj = Ticket.objects.get(pk=ticket)
        except Ticket.DoesNotExist:
            self.stderr.write(f"Ticket {ticket} not found.")
            raise SystemExit(1) from None

        # #801 SSOT: canonical earliest+locked policy (was -pk-latest
        # else an unlocked raw create); non-blank agent_id on miss.
        session = ticket_obj.resolve_phase_session(agent_id="phase-handoff")
        target = Task.ExecutionTarget.INTERACTIVE if interactive else Task.ExecutionTarget.HEADLESS
        task = Task.objects.create(
            ticket=ticket_obj,
            session=session,
            phase=phase,
            execution_target=target,
            execution_reason=body,
        )
        self.stdout.write(f"Created task {task.pk} (ticket {ticket_obj.pk}, phase={phase}, target={target}).")
        return {"task_id": task.pk, "ticket_id": ticket_obj.pk, "phase": phase, "execution_target": target}

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

    @command()
    def complete(
        self,
        task_id: Annotated[int, typer.Argument(help="Task ID (see `task_id` in `tasks list`).")],
        *,
        note: Annotated[
            str,
            typer.Option(help="Audit-trail reason recorded on a TaskAttempt (e.g. 'work landed via !6219')."),
        ] = "",
    ) -> None:
        """Mark a claimed task COMPLETED for work finished out-of-band (#1031).

        Drives the Task FSM ``claimed → completed`` (releasing the lease and
        auto-advancing the ticket). Idempotent: completing an already-completed
        task is a no-op with exit 0. Rejects a task in any non-``claimed`` state
        (``pending``, ``failed``) with a clear error.

        Fail-closed evidence gate (#1280): when ``--note`` ASSERTS an external
        outcome (merged / posted / shipped / deployed) it must also carry a
        resolvable artifact pointer (URL / SHA / ``!123`` / ``#123`` / note id /
        path), so a phantom "done" claim cannot be recorded without proof. A
        note with no outcome claim — or no note — is untouched.
        """
        from django.db import transaction  # noqa: PLC0415
        from django.utils import timezone  # noqa: PLC0415

        from teatree.core.completion_evidence import CompletionEvidenceError, check_completion_evidence  # noqa: PLC0415

        try:
            check_completion_evidence(note)
        except CompletionEvidenceError as exc:
            self.stderr.write(str(exc))
            raise SystemExit(1) from None

        with transaction.atomic():
            try:
                task = Task.objects.select_for_update().get(pk=task_id)
            except Task.DoesNotExist:
                self.stderr.write(f"Task {task_id} not found.")
                raise SystemExit(1) from None

            if task.status == Task.Status.COMPLETED:
                self.stdout.write(f"Task {task_id} already completed; nothing to do.")
                return

            if task.status != Task.Status.CLAIMED:
                self.stderr.write(
                    f"Task {task_id} is '{task.status}', not 'claimed'. Only a claimed task can be completed.",
                )
                raise SystemExit(1)

            if note.strip():
                TaskAttempt.objects.create(
                    task=task,
                    execution_target=task.execution_target,
                    ended_at=timezone.now(),
                    exit_code=0,
                    result={"complete_note": note},
                )
            task.complete()
        self.stdout.write(f"Task {task_id} completed.")

    @command(name="list")
    def list_tasks(
        self,
        *,
        status: Annotated[str | None, typer.Option(help="Filter by status")] = None,
        execution_target: Annotated[str | None, typer.Option(help="Filter by execution target")] = None,
        session: Annotated[
            bool,
            typer.Option(help="Scope to the current Claude session and group pending / claimed / done."),
        ] = False,
    ) -> list[TaskRow]:
        Task.objects.reap_stale_claims()
        if session:
            return self._list_session_todos(status=status, execution_target=execution_target)
        qs = Task.objects.all().order_by("pk")
        if status:
            qs = qs.filter(status=status)
        if execution_target:
            qs = qs.filter(execution_target=execution_target)
        rows = [_task_row(task) for task in qs]
        _render_tasks_table(rows, stream=cast("IO[str]", self.stdout))
        return rows

    def _list_session_todos(
        self,
        *,
        status: str | None,
        execution_target: str | None,
    ) -> list[TaskRow]:
        """Print the current Claude session's tasks, grouped by status (#todos).

        Sources from the teatree ``Task`` model scoped to the active Claude
        session (``Session.agent_id``), then merges the harness ``TodoWrite``
        list persisted to ``<session>.todos`` so a single view covers both the
        durable lifecycle tasks and the in-flight harness todos.
        """
        from teatree.core.session_identity import current_session_id  # noqa: PLC0415

        session_id = current_session_id()
        qs = Task.objects.for_claude_session(session_id)
        if status:
            qs = qs.filter(status=status)
        if execution_target:
            qs = qs.filter(execution_target=execution_target)
        rows = [_task_row(task) for task in qs]
        harness_todos = _read_harness_todos(session_id)
        _render_session_todos(
            rows,
            harness_todos=harness_todos,
            session_id=session_id,
            stream=cast("IO[str]", self.stdout),
        )
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


_STATUS_STYLES: dict[str, str] = {
    "pending": "yellow",
    "claimed": "cyan",
    "completed": "green",
    "failed": "red",
}

# Status → display group for the session-scoped ``--session`` view. ``claimed``
# is the loop's in-flight state, surfaced as "in_progress" to match the harness
# task-list vocabulary; ``failed`` is grouped under "completed" (terminal).
_TODO_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("pending", ("pending",)),
    ("in_progress", ("claimed",)),
    ("completed", ("completed", "failed")),
]


def _task_row(task: Task) -> TaskRow:
    return TaskRow(
        task_id=task.pk,
        ticket_id=task.ticket_id,  # ty: ignore[unresolved-attribute]
        status=task.status,
        execution_target=task.execution_target,
        phase=task.phase,
        execution_reason=task.execution_reason,
        claimed_by=task.claimed_by,
    )


def _read_harness_todos(session_id: str) -> list[tuple[str, str]]:
    """Read the harness ``TodoWrite`` list for *session_id* as ``(status, text)``.

    The hook persists one ``- [status] content`` line per todo to
    ``<state_dir>/<session>.todos``. Best-effort: a missing file or unreadable
    state dir yields an empty list (the task model rows still render).
    """
    import re  # noqa: PLC0415

    from teatree.agents.handover import get_claude_statusline_state_dir  # noqa: PLC0415

    if not session_id:
        return []
    path = get_claude_statusline_state_dir() / f"{session_id}.todos"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    line_re = re.compile(r"^- \[(?P<status>[^\]]*)\]\s*(?P<text>.+)$")
    todos: list[tuple[str, str]] = []
    for line in raw.splitlines():
        match = line_re.match(line.strip())
        if match:
            todos.append((match.group("status").strip() or "pending", match.group("text").strip()))
    return todos


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


def _render_session_todos(
    rows: list[TaskRow],
    *,
    harness_todos: list[tuple[str, str]],
    session_id: str,
    stream: IO[str] | None = None,
) -> None:
    """Render the current session's todos grouped pending / in_progress / completed."""
    console = Console(file=stream) if stream is not None else Console()
    if not session_id:
        console.print("[dim]No active Claude session — cannot scope todos to a session.[/dim]")
        return
    if not rows and not harness_todos:
        console.print("[dim]No todos for this session.[/dim]")
        return

    rows_by_status: dict[str, list[TaskRow]] = {}
    for row in rows:
        rows_by_status.setdefault(row["status"], []).append(row)

    for group, statuses in _TODO_GROUPS:
        group_rows = [row for status in statuses for row in rows_by_status.get(status, [])]
        group_todos = [text for status, text in harness_todos if _todo_group(status) == group]
        if not group_rows and not group_todos:
            continue
        style = _STATUS_STYLES.get(statuses[0], "")
        console.print(f"[bold {style}]{group}[/] ({len(group_rows) + len(group_todos)})" if style else group)
        for row in group_rows:
            phase = f" {row['phase']}" if row["phase"] else ""
            reason = row["execution_reason"] or "-"
            console.print(f"  task #{row['task_id']} (ticket #{row['ticket_id']}{phase}): {reason}")
        for text in group_todos:
            console.print(f"  [dim]todo:[/] {text}")


def _todo_group(status: str) -> str:
    """Map a harness todo status to a display group key.

    The harness already speaks ``pending`` / ``in_progress`` / ``completed``;
    any unknown status falls under ``pending`` so it is never silently dropped.
    """
    normalized = status.strip().lower()
    for group, _statuses in _TODO_GROUPS:
        if normalized == group:
            return group
    return "completed" if normalized in {"done", "complete"} else "pending"


def _build_claude_command(task: Task) -> list[str]:
    """Build the ``claude`` argv for an interactive task.

    Resumes the prior session when the task carries a Claude session UUID,
    otherwise starts a fresh session with the interactive system context
    pre-loaded via ``--append-system-prompt``.
    """
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        msg = "claude CLI is not installed"
        raise FileNotFoundError(msg)

    agent_id = task.session.agent_id if task.session else ""
    if agent_id and UUID_RE.match(agent_id):
        logger.info("Resuming claude session %s for task %s", agent_id, task.pk)
        return [claude_bin, "--resume", agent_id]

    overlay_skill_metadata = get_overlay().metadata.get_skill_metadata()
    skills = resolve_skill_bundle(phase=task.phase, overlay_skill_metadata=overlay_skill_metadata)
    system_context = build_interactive_context(task, skills=skills)
    return [claude_bin, "--append-system-prompt", system_context]


def _resolve_reason(*, reason: str, reason_file: pathlib.Path | None) -> str:
    if reason == "-":
        return sys.stdin.read()
    if reason:
        return reason
    if reason_file is not None:
        return reason_file.read_text()
    return ""


def _exec_inline(argv: list[str]) -> None:
    from teatree.utils.run import run_streamed  # noqa: PLC0415

    orig_cwd = os.environ.get("T3_ORIG_CWD", "")
    cwd = orig_cwd if orig_cwd and pathlib.Path(orig_cwd).is_dir() else None
    rc = run_streamed(argv, cwd=cwd, check=False)
    raise SystemExit(rc)
