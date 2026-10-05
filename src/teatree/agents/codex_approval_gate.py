"""Codex App Server approvals evaluated by the Claude PreToolUse router."""

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from teatree.agents.codex_app_server_options import CodexAppServerOptions
from teatree.agents.codex_router import run_router
from teatree.agents.shell_wrapper import unwrap_lone_shell

_ROUTER_FAILURE = "TeaTree PreToolUse router could not evaluate the action."
_DENY_EXIT = 2
_COMMAND_APPROVAL = "item/commandExecution/requestApproval"
_FILE_CHANGE_APPROVAL = "item/fileChange/requestApproval"
APPROVAL_METHODS = frozenset({_COMMAND_APPROVAL, _FILE_CHANGE_APPROVAL})


async def approval_decision(
    method: str,
    params: Mapping[str, Any],
    options: CodexAppServerOptions | None,
    file_changes: object = None,
) -> tuple[str, str | None]:
    """The decision for one request; a refusal reason is steered back to the model."""
    if options is None or options.sandbox_mode != "danger-full-access":
        return "decline", "Approval request has no worker write session."
    cwd = params.get("cwd") or options.core.cwd
    if not isinstance(cwd, str) or not await asyncio.to_thread(Path(cwd).is_dir):
        return "decline", "Approval request has no valid working directory."
    if method == _COMMAND_APPROVAL:
        command = params.get("command")
        if not isinstance(command, str) or not command.strip():
            return "decline", "Approval request has no command."
        return await _run_router("Bash", {"command": unwrap_lone_shell(command), "cwd": cwd}, cwd, params)
    if method == _FILE_CHANGE_APPROVAL:
        return await _approve_file_changes(file_changes, cwd, params)
    return "decline", "Unsupported approval request."


async def _approve_file_changes(changes: object, cwd: str, params: Mapping[str, Any]) -> tuple[str, str | None]:
    if not isinstance(changes, list) or not changes:
        return "decline", "Approval request has no file changes."
    for change in changes:
        if not isinstance(change, dict) or not isinstance(change.get("path"), str) or not change["path"]:
            return "decline", "Approval request has an invalid file path."
        kind = change.get("kind")
        if not isinstance(kind, dict) or kind.get("type") not in {"add", "delete", "update"}:
            return "decline", "Approval request has an invalid change kind."
        move = kind.get("move_path")
        if move is not None and not isinstance(move, str):
            return "decline", "Approval request has an invalid move path."
        targets = [(change["path"], "Write" if kind["type"] == "add" else "Edit")]
        if move:
            targets.append((move, "Write"))
        for path, tool in targets:
            tool_input = {"file_path": str((Path(cwd) / path).resolve())}
            if isinstance(change.get("diff"), str):
                tool_input["content" if tool == "Write" else "new_string"] = change["diff"]
            decision, reason = await _run_router(tool, tool_input, cwd, params)
            if decision != "accept":
                return decision, reason
    return "accept", None


async def _run_router(
    tool_name: str, tool_input: dict[str, str], cwd: str, params: Mapping[str, Any]
) -> tuple[str, str | None]:
    payload = {"session_id": params.get("threadId", ""), "tool_name": tool_name, "tool_input": tool_input}
    run = await run_router("PreToolUse", payload, cwd)
    if run is None or run.crashed or run.returncode not in {0, _DENY_EXIT}:
        return "decline", _ROUTER_FAILURE
    try:
        result = json.loads(run.stdout) if run.stdout.strip() else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "decline", _ROUTER_FAILURE
    return _router_verdict(run.returncode, result)


def _router_verdict(returncode: int, result: object) -> tuple[str, str | None]:
    if not isinstance(result, dict):
        return "decline", _ROUTER_FAILURE
    specific = result.get("hookSpecificOutput", {})
    if not isinstance(specific, dict):
        return "decline", _ROUTER_FAILURE
    decision = specific.get("permissionDecision", result.get("permissionDecision"))
    reason = specific.get("permissionDecisionReason", result.get("permissionDecisionReason"))
    if returncode == _DENY_EXIT or decision in {"deny", "block"} or result.get("continue") is False:
        return "decline", reason if isinstance(reason, str) and reason else _ROUTER_FAILURE
    if decision not in {None, "allow"}:
        return "decline", _ROUTER_FAILURE
    return "accept", None
