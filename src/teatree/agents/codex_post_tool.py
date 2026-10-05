"""Report completed Codex tools to the same hook router used for approvals."""

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from teatree.agents.codex_app_server_options import CodexAppServerOptions
from teatree.agents.codex_router import run_router

logger = logging.getLogger(__name__)


async def report_completed_item(item: Mapping[str, Any], thread_id: str, options: CodexAppServerOptions) -> None:
    """Record a completed command or file change with the router; a later approval normally waits for it."""
    if item.get("status") == "declined":
        return
    cwd = str(item.get("cwd") or options.core.cwd)
    item_type = item.get("type")
    if item_type == "commandExecution":
        command = item.get("command")
        if not isinstance(command, str):
            return
        output = str(item.get("aggregatedOutput") or "")
        await _send_post("Bash", {"command": command, "cwd": cwd}, output, cwd, thread_id)
        if item.get("status") == "completed" and item.get("exitCode") in {None, 0}:
            for path in _read_paths(item.get("commandActions"), cwd):
                await _send_post("Read", {"file_path": path}, output, cwd, thread_id)
    elif item_type == "fileChange":
        await _report_file_changes(item, cwd, thread_id)


async def _report_file_changes(item: Mapping[str, Any], cwd: str, thread_id: str) -> None:
    changes = item.get("changes")
    if not isinstance(changes, list):
        return
    for change in changes:
        if isinstance(change, dict) and isinstance(change.get("path"), str):
            kind = change.get("kind")
            tool = "Write" if isinstance(kind, dict) and kind.get("type") == "add" else "Edit"
            paths = [change["path"]]
            if isinstance(kind, dict) and isinstance(kind.get("move_path"), str):
                paths.append(kind["move_path"])
            for index, path in enumerate(paths):
                actual_tool = "Write" if index else tool
                tool_input = {"file_path": str((Path(cwd) / path).resolve())}
                if isinstance(change.get("diff"), str):
                    tool_input["content" if actual_tool == "Write" else "new_string"] = change["diff"]
                await _send_post(actual_tool, tool_input, str(item.get("status") or ""), cwd, thread_id)


def _read_paths(actions: object, cwd: str) -> tuple[str, ...]:
    # Codex reports commands as `/bin/bash -lc '...'`; its own parsed `read` actions name what was read.
    if not isinstance(actions, list):
        return ()
    return tuple(
        str((Path(cwd) / action["path"]).resolve())
        for action in actions
        if isinstance(action, dict)
        and action.get("type") == "read"
        and isinstance(action.get("path"), str)
        and action["path"]
    )


async def _send_post(tool: str, tool_input: dict[str, str], response: str, cwd: str, thread_id: str) -> None:
    payload = {"session_id": thread_id, "tool_name": tool, "tool_input": tool_input, "tool_response": response}
    run = await run_router("PostToolUse", payload, cwd)
    if run is None or run.returncode != 0 or run.crashed:
        logger.warning("Codex PostToolUse router failed for %s", tool)
