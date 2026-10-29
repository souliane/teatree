"""Deny tools on MCP servers other than teatree's own."""

from hooks.scripts.mcp_slack_write_guard import TEATREE_MCP_SERVERS

_DENY_REASON = (
    "BLOCKED: policy allows no MCP server beside teatree's own. "
    "Find the teatree replacement with `mcp__teatree__command_search`, "
    "or use the `t3` CLI (`t3 --help`). "
    "One-off escape: put `[gate-escape:foreign_mcp: <reason>]` in a string argument."
)

_RESOURCE_TOOLS = frozenset(
    {"ListMcpResources", "ListMcpResourcesTool", "ReadMcpResource", "ReadMcpResourceTool", "ReadMcpResourceDirTool"}
)


def is_foreign_mcp_tool(tool_name: str, tool_input: dict | None = None) -> bool:
    if tool_name in _RESOURCE_TOOLS:
        arguments = tool_input if isinstance(tool_input, dict) else {}
        servers = [arguments[key] for key in ("server", "server_name") if key in arguments]
        return not servers or any(
            not isinstance(server, str) or server not in TEATREE_MCP_SERVERS for server in servers
        )
    if not tool_name.startswith("mcp__"):
        return False
    server = tool_name.removeprefix("mcp__").rpartition("__")[0]
    return server not in TEATREE_MCP_SERVERS


def handle_block_foreign_mcp_tool(data: dict) -> bool:
    if not is_foreign_mcp_tool(data.get("tool_name", ""), data.get("tool_input")):
        return False
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 — router back-import at call time

    return _fail_open_or_deny(data, _DENY_REASON, gate_id="foreign_mcp")
