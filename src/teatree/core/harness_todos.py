"""Best-effort reader of the harness's OWN on-disk TODO store (domain-layer).

Reads a point-in-time disk snapshot of the harness's TODO store
(``~/.claude/tasks/<session>/*.json`` — the harness writes it, teatree does
not). It exists for recovery-snapshot consumers that run inside a hook
subprocess and genuinely cannot call the live ``TaskList`` harness tool: the
PreCompact durable snapshot (``hook_router._durable_session_snapshot``) and the
continuous stop-snapshotter (``teatree.core.stop_snapshot``). A lagging disk
read is the only option there and an acceptable one (a snapshot is a moment in
time anyway).

It is deliberately NOT used by the interactive ``/t3:checking`` path — that list
lives in the harness's live in-memory session state and is built dynamically
from the live ``TaskList`` tool (see ``tasks_session_view`` for why). This is a
stdlib-only DOMAIN module (no Django), so both the interface-layer CLI and the
domain-layer snapshotter reach it without a backwards layer edge.
"""

import json
import os
import pathlib


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


def read_harness_todos(session_id: str) -> list[tuple[str, str]]:
    """Read the session's harness TODO list as ``(status, text)`` — snapshots only.

    A best-effort, point-in-time read of the harness's OWN on-disk TODO store
    (``~/.claude/tasks/<session>/*.json``). Empty session id (no resolvable
    harness session) yields an empty list.

    Do NOT use this for the interactive ``/t3:checking`` list — it lags the live
    session. The agent builds that list from the live ``TaskList`` harness tool.
    """
    if not session_id:
        return []
    return _read_harness_todos_from_store(session_id)
