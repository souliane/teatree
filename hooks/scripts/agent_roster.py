r"""PostToolUse: persist the dispatched sub-agent roster to ``<session>.agents``.

The agentId is the handle ``SendMessage`` needs to resume/steer/collect a running
agent; it lives only in the conversation and is lost on auto-compaction, orphaning
the agent. Mirroring the #970 ``TodoWrite`` capture, every ``Agent`` PostToolUse
appends the agentId + its role/description so the PreCompact snapshot can quote
the roster back. Each line is ``<agentId>\t<role>`` — append-only and deduped on
agentId, so a multi-agent fan-out accumulates rather than clobbers.

Extracted whole from ``hook_router`` (the #2384 router-split pattern) so the
shrink-only dispatcher nets smaller; the router re-exports
:func:`handle_track_agents` into ``_HANDLERS`` unchanged.

Cold-import safe: stdlib only at module top. The state-file spine helpers stay in
the router and are back-imported lazily inside the handler body.
"""

import os
import sys
from pathlib import Path
from typing import TypedDict, cast

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# re-exports and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("agent_roster", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.agent_roster", sys.modules[__name__])


class AgentDispatchResponse(TypedDict, total=False):
    """The id-bearing keys of an ``Agent`` PostToolUse response.

    The harness response shape is not contractually fixed, so all three are
    optional and each is re-checked at runtime — this types the probe, it does
    not vouch for the payload.
    """

    agentId: str
    agent_id: str
    id: str


def _agent_id_from_response(tool_response: object) -> str:
    """Extract the dispatched agentId from an ``Agent`` PostToolUse payload.

    Probes the known id-bearing keys in precedence order. Returns ``""`` when
    none is present — the caller then falls back to scanning the harness tasks
    dir.
    """
    if not isinstance(tool_response, dict):
        return ""
    response = cast("AgentDispatchResponse", tool_response)
    for value in (response.get("agentId"), response.get("agent_id"), response.get("id")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _newest_task_agent_id() -> str:
    """Scan the harness tasks output dir for the newest ``a*`` task id.

    Fallback used only when the PostToolUse payload does not expose the agentId.
    The harness writes one ``<agentId>.output`` file per dispatched task under
    ``CLAUDE_TASKS_DIR`` (or ``~/.claude/tasks``); the dispatched sub-agent's id
    is ``a``-prefixed. Returns the most-recently-modified match, or ``""`` when
    the dir is absent / has no match. Never raises — capture must never block the
    orchestrator.
    """
    tasks_dir = Path(os.environ.get("CLAUDE_TASKS_DIR", str(Path.home() / ".claude" / "tasks")))
    try:
        candidates = [p for p in tasks_dir.glob("a*.output") if p.is_file()]
    except OSError:
        return ""
    if not candidates:
        return ""
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    return newest.stem


def handle_track_agents(data: dict) -> None:
    """Persist a dispatched ``Agent`` sub-agent's id + role to ``<session>.agents``.

    No-op for any other tool name. Prefers the agentId carried on the PostToolUse
    ``tool_response`` (``tool_result`` as a secondary payload key); falls back to
    the newest ``a*`` id under the harness tasks dir when the payload omits it.
    Append-only and deduped on agentId so a parallel fan-out of sub-agents all
    survive compaction.
    """
    if data.get("tool_name") != "Agent":
        return
    session_id = data.get("session_id", "")
    if not session_id:
        return

    agent_id = _agent_id_from_response(data.get("tool_response"))
    if not agent_id:
        agent_id = _agent_id_from_response(data.get("tool_result"))
    if not agent_id:
        agent_id = _newest_task_agent_id()
    if not agent_id:
        return

    tool_input = data.get("tool_input", {})
    role = str(tool_input.get("description") or tool_input.get("subagent_type") or "(no description)").strip()

    from hooks.scripts.hook_router import (  # noqa: PLC0415 — deferred back-import: the state-file spine stays in the router
        _append_line,
        _ensure_state_dir,
        _read_lines,
        _state_file,
    )

    _ensure_state_dir()
    agents_file = _state_file(session_id, "agents")
    if any(line.split("\t", 1)[0] == agent_id for line in _read_lines(agents_file)):
        return
    _append_line(agents_file, f"{agent_id}\t{role}")
