"""The ``tasks list --session`` view: the session-scoped teatree tasks.

This view renders **only** the teatree *tasks* — rows in the DB-backed
``Task`` model — scoped to the current harness session, grouped pending /
in_progress / completed.

It deliberately does NOT render the *harness TODO* list (the agent harness's
own ``TaskCreate`` / ``TaskUpdate`` items). That list lives in the harness's
live, in-memory session state, and the Task tools bypass ``PreToolUse`` /
``PostToolUse`` hooks (a known harness regression — see
``docs/claude-code-internals.md`` § 9), so a ``t3`` CLI subprocess can only
read a stale on-disk snapshot (``~/.claude/tasks/<session>/*.json``) that lags
the live session and is never reliably in sync. ``/t3:checking`` therefore builds
the harness half **dynamically from the live ``TaskList`` harness tool** — not
from this CLI. Keeping the CLI's session view scoped to the teatree ``Task``
rows means it can never masquerade as the live session todo list.

The best-effort on-disk harness-TODO reader that the recovery snapshots need
lives in the domain-layer :mod:`teatree.core.harness_todos` (``read_harness_todos``),
NOT here — a hook / domain consumer reaches it without a backwards layer edge
through this interface-layer command. There is NO teatree-written mirror of the
harness list (the old ``<session>.todos`` materialiser was removed — it was a
stale mistake-source that nothing load-bearing read). The reconciliation
discipline that keeps the LIVE harness TODO list faithful belongs to the
in-session agent, which applies ``/t3:checking`` § "Harness-TODO maintenance" (and
the ``tasks reconcile-checklist`` emitter) with its own ``TaskList`` /
``TaskUpdate`` / ``TaskCreate`` tools. The fix is to keep the best-effort disk
read OUT of the interactive ``/t3:checking`` path, where the agent must read the
live list.
"""

from typing import IO, TypedDict

from rich.console import Console
from rich.table import Table

from teatree.core.ref_render import render_ref, short_title


class TaskRow(TypedDict):
    task_id: int
    ticket_id: int
    ticket_title: str
    status: str
    phase: str
    execution_reason: str
    claimed_by: str
    # #3957: WHY the task failed, as distinct from ``execution_reason`` (why it was
    # scheduled). Blank on every non-failed row. ``failure_environmental`` separates an
    # infrastructure fault from a defect in the work, so an operator scanning the listing
    # can tell a genuine review failure from a lost lease without opening anything.
    failure_kind: str
    failure_reason: str
    failure_environmental: bool


STATUS_STYLES: dict[str, str] = {
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


def render_session_view(
    rows: list[TaskRow],
    *,
    session_id: str,
    stream: IO[str] | None = None,
) -> None:
    """Render the session's teatree tasks, grouped pending / in_progress / completed.

    Only the teatree ``Task`` rows render here — the harness TODO list is built
    from the live ``TaskList`` harness tool by ``/t3:checking``, never from this
    CLI (a subprocess can only read a stale on-disk snapshot of it).
    """
    console = Console(file=stream) if stream is not None else Console()
    if not session_id:
        console.print("[dim]No active harness session — cannot scope teatree tasks to a session.[/dim]")
        return
    if not rows:
        console.print("[dim]No teatree tasks for this session.[/dim]")
        return

    _render_teatree_tasks_section(rows, console=console)


def _render_teatree_tasks_section(rows: list[TaskRow], *, console: Console) -> None:
    rows_by_status: dict[str, list[TaskRow]] = {}
    for row in rows:
        rows_by_status.setdefault(row["status"], []).append(row)
    console.print(f"[bold]teatree tasks[/] ({len(rows)})")
    for group, statuses in _TODO_GROUPS:
        group_rows = [row for status in statuses for row in rows_by_status.get(status, [])]
        if not group_rows:
            continue
        style = STATUS_STYLES.get(statuses[0], "")
        console.print(f"  [bold {style}]{group}[/] ({len(group_rows)})" if style else f"  {group}")
        for row in group_rows:
            phase = f" {row['phase']}" if row["phase"] else ""
            reason = row["execution_reason"] or "-"
            ticket_ref = render_ref(f"#{row['ticket_id']}", title=row["ticket_title"])
            console.print(f"    task TODO-{row['task_id']} (ticket {ticket_ref}{phase}): {reason}")
            # A failed row says WHY on its own line — the scheduling reason above it is
            # not the failure, and reading it as one is the #3957 defect.
            if row["failure_kind"]:
                console.print(f"      [red]failed[/] [bold]{row['failure_kind']}[/]: {row['failure_reason']}")


# The fixed reconcile-discipline steps. The harness TODO list is live, in-memory
# state the in-session agent owns through its ``TaskList`` / ``TaskUpdate`` /
# ``TaskCreate`` tools; teatree cannot read or write it (the Task tools bypass
# ``PreToolUse`` / ``PostToolUse`` hooks). So this is what a deterministic helper
# CAN be — the checklist the agent applies with its own tools, not a writer.
_RECONCILE_STEPS: tuple[str, ...] = (
    "Call [bold]TaskList[/] now and read the live, in-memory harness TODO list.",
    (
        "[bold]Reconcile[/] it against this conversation: every still-open ask the user "
        "made — and every step you committed to — has a TODO. Add the forgotten ones with "
        "[bold]TaskCreate[/]."
    ),
    (
        "[bold]Consolidate / dedupe[/]: collapse duplicate or overlapping items into one "
        "with [bold]TaskUpdate[/]; a single faithful item beats three half-stated ones."
    ),
    (
        "Mark every finished item [bold]completed[/] with [bold]TaskUpdate[/], and the one "
        "you are on [bold]in_progress[/] — never leave a done item pending or a stale "
        "in_progress lingering."
    ),
)


def render_reconcile_checklist(
    rows: list[TaskRow],
    *,
    session_id: str,
    stream: IO[str] | None = None,
) -> None:
    """Render the harness-TODO reconciliation checklist for the in-session agent.

    A pure render — this function only writes to ``stream``: teatree cannot
    touch the live harness TODO list (the Task tools bypass the hooks), so it
    prints the fixed reconcile / dedupe / complete steps the agent applies with
    its own ``TaskList`` / ``TaskUpdate`` / ``TaskCreate`` tools. The session's
    open teatree ``Task`` rows print below as completion anchors (work the loop
    tracked that the agent may need to mark done). The calling command's only
    DB write is the standard stale-claim reaper (a CLAIMED→FAILED CAS on an
    already-expired lease); this render makes no reconciliation write at all.
    """
    console = Console(file=stream) if stream is not None else Console()
    console.print("[bold]Harness-TODO reconciliation[/] — apply with your OWN harness tools (read-only emitter):")
    for index, step in enumerate(_RECONCILE_STEPS, start=1):
        console.print(f"  {index}. {step}")

    open_rows = [row for row in rows if row["status"] in {"pending", "claimed"}]
    if not session_id:
        console.print(
            "[dim]No active harness session — no session-scoped teatree tasks to cross-check; "
            "still apply the steps above.[/dim]",
        )
        return
    if not open_rows:
        console.print("[dim]No open teatree tasks for this session to cross-check against.[/dim]")
        return
    console.print(f"[bold]Open teatree tasks this session[/] ({len(open_rows)}) — completion anchors:")
    for row in open_rows:
        phase = f" {row['phase']}" if row["phase"] else ""
        reason = row["execution_reason"] or "-"
        ticket_ref = render_ref(f"#{row['ticket_id']}", title=row["ticket_title"])
        console.print(f"  task TODO-{row['task_id']} (ticket {ticket_ref}{phase}): {reason}")


# A redirected/captured stream has no terminal width; rich then defaults to 80
# cols and crushes the Title column (#2092). Give piped output a generous fixed
# width so every column renders untruncated; a real terminal keeps its own width.
_TABLE_PIPE_WIDTH = 160


def render_tasks_table(rows: list[TaskRow], *, stream: IO[str] | None = None) -> None:
    console = Console(file=stream, width=_TABLE_PIPE_WIDTH) if stream is not None else Console()
    if not rows:
        console.print("[dim]No tasks.[/dim]")
        return

    table = Table(title=f"teatree tasks ({len(rows)})", show_lines=False)
    table.add_column("ID", justify="right", style="bold")
    table.add_column("Ticket", justify="right")
    table.add_column("Title", overflow="ellipsis", max_width=48)
    table.add_column("Status")
    table.add_column("Target")
    table.add_column("Phase")
    table.add_column("Claimed by")
    # Two REASON columns, because they answer different questions and conflating them is
    # the #3957 defect: "Scheduled for" is why the task was dispatched, "Failed because"
    # is why it ended. A failed row that showed only the former looked cause-less.
    table.add_column("Scheduled for", overflow="fold", max_width=44)
    table.add_column("Failed because", overflow="fold", max_width=44)

    for row in rows:
        status = row["status"]
        style = STATUS_STYLES.get(status, "")
        table.add_row(
            str(row["task_id"]),
            str(row["ticket_id"]),
            short_title(row["ticket_title"]) or "-",
            f"[{style}]{status}[/]" if style else status,
            row["phase"] or "-",
            row["claimed_by"] or "-",
            row["execution_reason"] or "-",
            _failure_cell(row),
        )

    console.print(table)


def _failure_cell(row: TaskRow) -> str:
    """The named cause of a failed row, or ``-`` when it did not fail (#3957).

    The kind leads so a scan down the column groups causes without reading prose; the
    environmental marker tells the operator whether re-running could possibly help.
    """
    if not row["failure_kind"]:
        return "-"
    marker = "env" if row["failure_environmental"] else "deterministic"
    return f"[bold]{row['failure_kind']}[/] ({marker})\n{row['failure_reason']}"
