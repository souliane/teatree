"""Audit an inline gate escape through the control-plane CLI before allowing a call."""

import json
import sys
from pathlib import Path

from hooks.scripts.credential_redaction import redact_credentials
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


def _refused(gate_id: str, cause: str) -> bool:
    sys.stderr.write(f"Gate {gate_id} escape refused: {cause}.\n")
    return False


def _audit_request(data: dict, gate_id: str, turn_text: str | None) -> dict[str, str] | str | None:
    """The scope to audit, the cause it is refused, or ``None`` when the call carries no token for *gate_id*."""
    with teatree_src_on_path():
        from teatree.hooks.gate_escape import (  # noqa: PLC0415 — path setup
            EscapePolicy,
            action_digest,
            action_excerpt,
            find_escape,
            policy_for,
        )

        value = turn_text if turn_text is not None else data.get("tool_input", {})
        reason = find_escape(value, gate_id)
        if reason is None:
            return None
        if policy_for(gate_id) is EscapePolicy.NEVER:
            return "this gate never accepts an escape"
        session_id = data.get("session_id")
        cwd = data.get("cwd")
        if not isinstance(session_id, str) or not session_id or not isinstance(cwd, str) or not cwd:
            return "the hook payload has no session id or working directory"
        return {
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
            "excerpt": redact_credentials(action_excerpt(value)),
        }


def escape_allows(data: dict, gate_id: str, *, turn_text: str | None = None) -> bool:
    try:
        request = _audit_request(data, gate_id, turn_text)
        if request is None:
            return False
        if isinstance(request, str):
            return _refused(gate_id, request)
        argv = t3_argv("tool", "gate-escape")
        if argv is None:
            return _refused(gate_id, "t3 is not on PATH, so the audit cannot be written")
        result = run_t3(argv, timeout=20, stdin_text=json.dumps(request), budget=remaining_timeout_s)
        verdict = json.loads(result.stdout) if result.stdout.strip() else {}
        if result.returncode != 0 or verdict.get("allowed") is not True:
            return _refused(gate_id, str(verdict.get("reason") or f"the audit exited {result.returncode}"))
    except Exception as exc:  # noqa: BLE001 — an unrecorded escape never suppresses a deny
        return _refused(gate_id, type(exc).__name__)
    sys.stderr.write(f"Gate {gate_id} escape recorded for this action.\n")
    return True
