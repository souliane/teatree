"""Per-session ``UserPromptSubmit`` housekeeping nudges.

Bare sibling of ``hook_router`` (hooks/CLAUDE.md: the router is a shrink-only
module-health-capped god-module, so handler bodies live beside it and are
imported into its ``_HANDLERS`` chain). Moved out of the router unchanged to
make room for the standing-directive delivery in ``loop_registrations`` (#4166).

Everything here is ADVISORY: it prints ``additionalContext`` and never emits a
deny, so it can never block tool use.
"""

_TODO_FRESHNESS_NUDGE = (
    "Session housekeeping: keep the task/TODO list current. "
    "Reflect finished work as completed and surface any newly discovered work "
    "as its own task before continuing."
)


def handle_todo_freshness_nudge(data: dict) -> None:
    """Once per session, nudge keeping the task/TODO list current.

    Ordinary per-session housekeeping — fires in-session, never as a sub-agent
    and unrelated to the monitor/work-trigger loop. Idempotent via a per-session
    ``<session>.todo-nudged`` marker, mirroring the loop-pending precedent.
    """
    from hooks.scripts.hook_router import _ensure_state_dir, _state_file  # noqa: PLC0415 deferred back-import

    session_id = data.get("session_id", "")
    if not session_id:
        return
    _ensure_state_dir()
    marker = _state_file(session_id, "todo-nudged")
    if marker.exists():
        return
    marker.write_text("1", encoding="utf-8")
    print(_TODO_FRESHNESS_NUDGE)  # noqa: T201 — hook writes its protocol output to stdout


__all__ = ["handle_todo_freshness_nudge"]
