"""The governed configuration write exposed through MCP."""

from typing import Any

from asgiref.sync import sync_to_async
from mcp.server.mcpserver.exceptions import ToolError

from teatree.config.setting_taxonomy import owner_only_reason
from teatree.mcp.write_tool_run import run_command

# F9.1: this local cache bound delegates no authority despite its allowlist name.
MCP_SETTABLE_OK: frozenset[str] = frozenset({"disk_cache_allowlist"})


def refuse_reason(key: str) -> str:
    return owner_only_reason(key)


async def _config_setting_set(key: str, value: str, *, overlay: str = "") -> dict[str, Any]:
    """Set a plain config value; authorization, posture, and leak-scrub keys are refused."""
    if reason := refuse_reason(key):
        msg = f"refused: {key} is not MCP-settable ({reason})"
        raise ToolError(msg)

    def _set() -> dict[str, Any]:
        run_command("config_setting", "set", key, value, overlay=overlay)
        return {"ok": True, "key": key, "overlay": overlay}

    return await sync_to_async(_set, thread_sensitive=True)()
