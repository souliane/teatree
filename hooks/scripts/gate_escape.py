"""Audit an inline gate escape through the control-plane CLI before allowing a call."""

import json
import sys
from pathlib import Path

from hooks.scripts.foreign_branch_push_git import git_probe
from hooks.scripts.hook_budget import remaining_timeout_s
from hooks.scripts.managed_repo import teatree_src_on_path
from hooks.scripts.t3_invocation import run_t3, t3_argv

_NOT_A_REPOSITORY_EXIT_CODE = 128


def _head(cwd: str) -> str:
    result = git_probe(cwd, "rev-parse", "HEAD", timeout=2.0)
    if result.ok:
        return result.out
    if result.code == _NOT_A_REPOSITORY_EXIT_CODE and "not a git repository" in result.err:
        return ""
    message = "the gate escape head could not be read"
    raise OSError(message)


def _subject(data: dict) -> str:
    tool_input = data.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("file_path", "path"):
            if isinstance(path := tool_input.get(key), str) and path:
                return str(Path(path).resolve())
    return str(data.get("cwd") or "")


def escape_requested(data: dict, gate_id: str) -> bool:
    value = data.get("tool_input", {})
    try:
        with teatree_src_on_path():
            from teatree.hooks.gate_escape import find_escape  # noqa: PLC0415 — path setup precedes core import

            return find_escape(value, gate_id) is not None
    except Exception:  # noqa: BLE001 — an unreadable parser must not unlock the breaker
        return "[gate-escape:" in json.dumps(value, default=str)


def escape_allows(data: dict, gate_id: str, *, turn_text: str | None = None) -> bool:
    try:
        with teatree_src_on_path():
            from teatree.hooks.gate_escape import (  # noqa: PLC0415 — path setup
                EscapePolicy,
                action_digest,
                find_escape,
                policy_for,
            )

            value = turn_text if turn_text is not None else data.get("tool_input", {})
            reason = find_escape(value, gate_id)
            if reason is None:
                return False
            if policy_for(gate_id) is EscapePolicy.NEVER:
                sys.stderr.write(f"Gate {gate_id} does not accept an escape.\n")
                return False
            session_id = data.get("session_id")
            cwd = data.get("cwd")
            if not isinstance(session_id, str) or not session_id or not isinstance(cwd, str) or not cwd:
                return False
            scope = {
                "gate": gate_id,
                "reason": reason,
                "actor": str(data.get("agent_id") or f"agent:{session_id}"),
                "subject": _subject(data),
                "head": _head(cwd),
                "action_digest": action_digest(
                    str(data.get("tool_name") or ("Stop" if turn_text is not None else "")), value
                ),
                "session_id": session_id,
                "call_id": str(data.get("tool_use_id") or ""),
            }
        argv = t3_argv("tool", "gate-escape")
        if argv is None:
            sys.stderr.write(f"Gate {gate_id} escape refused: t3 is not on PATH, so the audit cannot be written.\n")
            return False
        result = run_t3(
            argv,
            timeout=20,
            stdin_text=json.dumps(scope),
            budget=remaining_timeout_s,
        )
        allowed = result.returncode == 0 and json.loads(result.stdout).get("allowed") is True
        if allowed:
            sys.stderr.write(f"Gate {gate_id} escape recorded for this action.\n")
    except Exception as exc:  # noqa: BLE001 — an unrecorded escape never suppresses a deny
        sys.stderr.write(f"Gate {gate_id} escape refused: {type(exc).__name__}.\n")
        return False
    return allowed
