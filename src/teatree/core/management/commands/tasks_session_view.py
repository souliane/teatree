"""The ``tasks list --session`` view: harness TODOs + teatree tasks.

Two distinct stores render here under separate headings, never merged. A
*teatree task* is a row in the DB-backed ``Task`` model. A *harness TODO*
is the agent harness's own working list (the ``TaskCreate`` /
``TaskUpdate`` items, formerly ``TodoWrite``).

The harness TODO list is read from the harness task store
(``CLAUDE_TASKS_DIR`` / ``~/.claude/tasks/<session>/*.json``), the
authoritative source: the legacy ``TodoWrite`` PostToolUse capture stopped
firing once the harness migrated to the ``TaskCreate`` / ``TaskUpdate``
tools, which bypass ``PostToolUse`` entirely. The legacy state file is a
fallback for any harness build still emitting ``TodoWrite``.
"""

import os
import pathlib
from typing import IO, TypedDict

from rich.console import Console


class TaskRow(TypedDict):
    task_id: int
    ticket_id: int
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
    """Read the current session's harness TODO list as ``(status, text)``.

    Prefers the harness task store; falls back to the legacy ``TodoWrite`` state
    file. Empty session id (no resolvable harness session) yields an empty list.
    """
    if not session_id:
        return []
    return _read_harness_todos_from_store(session_id) or _read_legacy_todowrite_state(session_id)


def render_session_todos(
    rows: list[TaskRow],
    *,
    harness_todos: list[tuple[str, str]],
    session_id: str,
    stream: IO[str] | None = None,
) -> None:
    """Render the session's harness TODOs and teatree tasks as two labeled sections.

    The harness TODO list (harness ``TaskCreate`` items) and the teatree tasks
    (DB-backed ``Task`` rows) are distinct stores; each renders under its own
    heading, grouped pending / in_progress / completed.
    """
    console = Console(file=stream) if stream is not None else Console()
    if not session_id:
        console.print("[dim]No active harness session — cannot scope todos to a session.[/dim]")
        return
    if not rows and not harness_todos:
        console.print("[dim]No todos for this session.[/dim]")
        return

    _render_harness_todos_section(harness_todos, console=console)
    _render_teatree_tasks_section(rows, console=console)


def _render_harness_todos_section(harness_todos: list[tuple[str, str]], *, console: Console) -> None:
    if not harness_todos:
        return
    console.print(f"[bold]harness TODOs[/] ({len(harness_todos)})")
    for group, statuses in _TODO_GROUPS:
        group_todos = [text for status, text in harness_todos if _todo_group(status) == group]
        if not group_todos:
            continue
        style = STATUS_STYLES.get(statuses[0], "")
        console.print(f"  [bold {style}]{group}[/] ({len(group_todos)})" if style else f"  {group}")
        for text in group_todos:
            console.print(f"    todo: {text}")


def _render_teatree_tasks_section(rows: list[TaskRow], *, console: Console) -> None:
    if not rows:
        return
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
            console.print(f"    task TODO-{row['task_id']} (ticket #{row['ticket_id']}{phase}): {reason}")


def _todo_group(status: str) -> str:
    """Map a harness TODO status to a display group key.

    The harness already speaks ``pending`` / ``in_progress`` / ``completed``;
    any unknown status falls under ``pending`` so it is never silently dropped.
    """
    normalized = status.strip().lower()
    for group, _statuses in _TODO_GROUPS:
        if normalized == group:
            return group
    return "completed" if normalized in {"done", "complete"} else "pending"
