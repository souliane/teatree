"""The active Claude session id — the loop's session-scoped identity (#1073).

Loop ownership is keyed by the *Claude session* that started the loop, NOT
the OS pid: ``loop_tick`` re-acquires the per-tick mutex with a fresh
``pid-<pid>`` every tick, so a pid-keyed identity rests unowned between
ticks and any session running ``t3 loop tick`` would win the unowned CAS
and do loop work (the #1073 hijack). The session id is stable across a
session's ticks and across context compaction, so it is the correct
identity for the persistent ``loop-owner`` claim.

This is the single source of truth for that primitive; both
``teatree.loop.session_identity`` (the loop-side callers) and
``teatree.outbound_claim`` (``_resolve_agent_session_id``) re-export it.
It lives in ``teatree.core`` rather than ``teatree.loop`` because the
module-boundary graph forbids ``teatree.outbound_claim`` (core-only)
depending on ``teatree.loop``; ``core`` is the lowest common module both
re-exporters are allowed to reach.

#1107 — the headline incident root cause: Claude Code delivers the
session id ONLY in the hook JSON payload, never as an env var inside a
Bash-tool subprocess, so in agent-driven mode both env vars are empty and
``current_session_id()`` returned ``""`` → ``t3 loop claim`` hard-refused
→ loop-owner could never be claimed → every owner-gated slot (#1075
reactive answer, self-improve, the claim-next spawn pump) was permanently
dead (131 user DMs reacted/answered never). The precedence is now:

    ``CLAUDE_SESSION_ID`` → ``T3_LOOP_SESSION_ID`` → loop-registry → ``""``

The registry fallback is correct-not-hack: ``loop-registry.json``'s
``t3-loop-tick-owner`` record IS the durable owner-identity source that
``_session_owns_loop`` (the gate consumers) already trust; making the
claim path read the same source removes an inconsistency rather than
inventing a new identity. The path resolution mirrors
``loop_slack_answer._session_owns_loop`` exactly (and
``hook_router`` which writes the record). The module-boundary graph
forbids ``teatree.core`` importing ``teatree.loop``/hooks, so the
registry key constant is deliberately redeclared here (same accepted
value-duplication rationale as ``loop_slack_answer``'s ``"t3-loop-tick
-owner"`` literal); it is read with only ``os``/``pathlib`` + ``json``
and fails open (any OSError/JSON error → ``""``).
"""

import json
import os
from pathlib import Path

# Deliberately redeclared (not imported) — ``teatree.core`` must not
# depend on ``teatree.loop``/hooks. Mirrors ``hook_router._OWNER_LOOP``
# and the literal already accepted in ``loop_slack_answer``. The
# ``gitleaks:allow`` is a false-positive suppression: a registry slot
# name, not a credential (same literal lives in ``loop_slack_answer.py``
# line 51 as a dict-key arg and goes unflagged there).
_OWNER_KEY = "t3-loop-tick-owner"  # gitleaks:allow


def _loop_registry_path() -> Path:
    """Resolve ``loop-registry.json`` exactly like ``loop_slack_answer``.

    ``T3_LOOP_REGISTRY_DIR`` env → ``XDG_DATA_HOME/teatree`` →
    ``~/.local/share/teatree`` (mirrors ``hook_router``'s writer).
    """
    base_env = os.environ.get("T3_LOOP_REGISTRY_DIR")
    if base_env:
        return Path(base_env) / "loop-registry.json"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "teatree" / "loop-registry.json"


def _session_id_from_loop_registry() -> str:
    """Read the tick-owner's session id from the loop registry, ``""`` on any error."""
    path = _loop_registry_path()
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return ""
    owner = data.get(_OWNER_KEY) if isinstance(data, dict) else None
    if not isinstance(owner, dict):
        return ""
    return owner.get("session_id") or ""


def current_session_id() -> str:
    """The active Claude session id, or ``""`` when not resolvable.

    ``CLAUDE_SESSION_ID`` is set by Claude Code; ``T3_LOOP_SESSION_ID`` is
    the test/manual override. When both are absent (#1107: agent-driven
    Bash-tool subprocesses never see the id as an env var) the loop
    registry's ``t3-loop-tick-owner`` record is the lowest-precedence
    fallback. Empty string means anonymous (no session) — the loop-owner
    gate treats an anonymous caller as a non-owner whenever a live owner
    exists.
    """
    return (
        os.environ.get("CLAUDE_SESSION_ID")
        or os.environ.get("T3_LOOP_SESSION_ID")
        or _session_id_from_loop_registry()
        or ""
    )


__all__ = ["current_session_id"]
