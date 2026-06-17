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
the live session and is never reliably in sync. ``/t3:todos`` therefore builds
the harness half **dynamically from the live ``TaskList`` harness tool** — not
from this CLI. Keeping the CLI's session view scoped to the teatree ``Task``
rows means it can never masquerade as the live session todo list.

``read_harness_todos`` (and its store readers) remain here for the **hook**
consumers that genuinely cannot call the live ``TaskList`` tool — the
PreCompact recovery snapshot and the statusline materialiser
(``hook_router.handle_track_todos`` / ``_write_recovery_snapshot``). Those are
best-effort, point-in-time captures inside a hook subprocess; a lagging disk
read is the only option there and an acceptable one (a snapshot is a moment in
time anyway). The fix is to keep that best-effort disk read OUT of the
interactive ``/t3:todos`` path, where the agent can and must read the live list.
"""

import os
import pathlib
from typing import IO, TypedDict

from rich.console import Console

from teatree.core.ref_render import render_ref


class TaskRow(TypedDict):
    task_id: int
    ticket_id: int
    ticket_title: str
    status: str
    execution_target: str
    phase: str
    execution_reason: str
    claimed_by: str


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
    from the live ``TaskList`` harness tool by ``/t3:todos``, never from this
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


# ── Harness TODO store readers — for HOOK consumers only ─────────────────────
#
# These read a best-effort, point-in-time disk snapshot of the harness TODO
# list. They exist for the PreCompact recovery snapshot and the statusline
# materialiser, which run inside a hook subprocess and genuinely cannot call the
# live ``TaskList`` harness tool. They are deliberately NOT used by the
# interactive ``/t3:todos`` path — see this module's docstring.


def _harness_tasks_dir() -> pathlib.Path:
    """The harness TODO store root (``CLAUDE_TASKS_DIR`` env or ``~/.claude/tasks``).

    Mirrors the resolution in ``hooks/scripts/hook_router._newest_task_agent_id`` —
    the hooks module cannot be imported from ``teatree.core`` (module-boundary
    graph), so the path is resolved here with stdlib only.
    """
    configured = os.environ.get("CLAUDE_TASKS_DIR")
    if configured:
        return pathlib.Path(configured)
    return pathlib.Path.home() / ".claude" / "tasks"


def _read_harness_todos_from_store(session_id: str) -> list[tuple[str, str]]:
    """Read the harness TODO list for *session_id* from the harness task store.

    The harness persists one ``<task-number>.json`` per harness TODO under
    ``<tasks_dir>/<session_id>/`` with ``subject`` / ``status`` fields.
    Best-effort: an absent dir or unreadable file yields an empty list.
    """
    import json  # noqa: PLC0415

    session_dir = _harness_tasks_dir() / session_id
    try:
        files = sorted(session_dir.glob("*.json"), key=lambda p: (len(p.stem), p.stem))
    except OSError:
        return []
    todos: list[tuple[str, str]] = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        subject = str(payload.get("subject", "")).strip()
        if not subject:
            continue
        status = str(payload.get("status", "pending")).strip() or "pending"
        todos.append((status, subject))
    return todos


def _read_legacy_todowrite_state(session_id: str) -> list[tuple[str, str]]:
    """Read the legacy ``TodoWrite`` state file as ``(status, text)``.

    The deprecated PostToolUse hook persisted one ``- [status] content`` line
    per todo to ``<state_dir>/<session>.todos``. Retained as a fallback;
    :func:`_read_harness_todos_from_store` is the primary source.
    """
    import re  # noqa: PLC0415

    from teatree.agents.handover import get_claude_statusline_state_dir  # noqa: PLC0415

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


def read_harness_todos(session_id: str) -> list[tuple[str, str]]:
    """Read the session's harness TODO list as ``(status, text)`` — HOOK consumers only.

    A best-effort, point-in-time disk snapshot for the PreCompact recovery
    snapshot and the statusline materialiser. Prefers the harness task store;
    falls back to the legacy ``TodoWrite`` state file. Empty session id (no
    resolvable harness session) yields an empty list.

    Do NOT use this for the interactive ``/t3:todos`` list — it lags the live
    session. The agent builds that list from the live ``TaskList`` harness tool.
    """
    if not session_id:
        return []
    return _read_harness_todos_from_store(session_id) or _read_legacy_todowrite_state(session_id)
