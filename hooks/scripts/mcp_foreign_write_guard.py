"""Deny direct writes through MCP servers other than teatree's own."""

import re
import sys

sys.modules.setdefault("mcp_foreign_write_guard", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.mcp_foreign_write_guard", sys.modules[__name__])

_SLACK_READ_VERBS: frozenset[str] = frozenset(
    {
        "fetch",
        "get",
        "history",
        "info",
        "list",
        "members",
        "permalink",
        "profile",
        "read",
        "replies",
        "search",
        "view",
    }
)
_FOREIGN_READ_VERBS = _SLACK_READ_VERBS | {"query", "retrieve"}
_WRITE_VERBS: frozenset[str] = frozenset(
    {
        "add",
        "append",
        "archive",
        "change",
        "create",
        "delete",
        "edit",
        "invite",
        "modify",
        "move",
        "patch",
        "post",
        "publish",
        "react",
        "remove",
        "rename",
        "replace",
        "schedule",
        "send",
        "set",
        "update",
        "upload",
        "write",
    }
)

_NAME_WORD_RE = re.compile(r"[a-z]+|[A-Z][a-z]*")

_SLACK_DENY_REASON = (
    "BLOCKED: a direct MCP Slack write bypasses teatree's Slack egress chokepoint "
    "(on-behalf gate, voice classifier, verify-by-re-read). Route it through the "
    "`t3` CLI instead: DM the user with `t3 teatree notify send -` (bot token); post to "
    "a colleague channel with `t3 <overlay> notify post --channel <id> --text <body>` "
    "(on-behalf gated); react with `t3 slack react`; comment on an MR/PR with "
    "`t3 review post-comment <repo> <mr> --body-file <abspath>` (top-level `t3 review`, "
    "never overlay-scoped). Recognised Slack MCP READS "
    "(get/list/search/history/info/read/view/fetch/replies/members) are unaffected; a "
    "Slack MCP tool this gate does not recognise is treated as a WRITE, so a genuine "
    "read it has not seen lands here too. One-off escape: put `[gate-escape:mcp_foreign_write: <reason>]` "
    "in the message text."
)
_NOTION_DENY_REASON = (
    "BLOCKED: a direct Notion MCP write bypasses teatree's page liveness and write gates. "
    "Use `t3 notion fetch`, `t3 notion create`, `t3 notion replace`, `t3 notion append`, "
    "`t3 notion comments`, `t3 notion comment post`, `t3 notion section replace`, or "
    "`t3 notion query` through teatree's backend. "
    "An unrecognised tool is treated as a WRITE. One-off escape: "
    "`[gate-escape:mcp_foreign_write: <reason>]`."
)
_OTHER_DENY_REASON = (
    "BLOCKED: a direct MCP write on another server bypasses teatree's gates. "
    "Use teatree's own MCP server or find the `t3` route with `command_search` or `t3 --help`. "
    "An unrecognised tool is treated as a WRITE. One-off escape: "
    "`[gate-escape:mcp_foreign_write: <reason>]`."
)
_REDIRECTS = (("slack", _SLACK_DENY_REASON), ("notion", _NOTION_DENY_REASON))


#: teatree's own server as a client names it: bare (``.mcp.json``) or plugin-scoped (``plugin_<plugin>_<server>``).
TEATREE_MCP_SERVERS: frozenset[str] = frozenset({"teatree", "plugin_t3_teatree"})


def is_slack_mcp_tool(tool_name: str) -> bool:
    """Whether *tool_name* is a Slack MCP tool (``mcp__*slack*``) on a server other than teatree's."""
    return is_foreign_mcp_tool(tool_name) and "slack" in tool_name.lower()


def is_teatree_mcp_tool(tool_name: str) -> bool:
    """Whether the tool is served by teatree's exact registered server name."""
    return tool_name.startswith("mcp__") and tool_name.removeprefix("mcp__").rpartition("__")[0] in TEATREE_MCP_SERVERS


def is_foreign_mcp_tool(tool_name: str) -> bool:
    return tool_name.startswith("mcp__") and not is_teatree_mcp_tool(tool_name)


def _has_read_verb(tool_name: str, read_verbs: frozenset[str]) -> bool:
    suffix = tool_name.rsplit("__", 1)[-1]
    words = {word.lower() for word in _NAME_WORD_RE.findall(suffix)}
    return bool(words & read_verbs) and not bool(words & _WRITE_VERBS)


def is_foreign_mcp_write(tool_name: str) -> bool:
    if not is_foreign_mcp_tool(tool_name):
        return False
    read_verbs = _SLACK_READ_VERBS if is_slack_mcp_tool(tool_name) else _FOREIGN_READ_VERBS
    return not _has_read_verb(tool_name, read_verbs)


def is_slack_mcp_write(tool_name: str) -> bool:
    return is_slack_mcp_tool(tool_name) and not _has_read_verb(tool_name, _SLACK_READ_VERBS)


def _slack_gate_enabled() -> bool:
    """The existing Slack switch still controls Slack writes only."""
    try:
        from hooks.scripts.hook_router import _teatree_bool_setting  # noqa: PLC0415 deferred back-import

        return _teatree_bool_setting("mcp_slack_write_gate_enabled")
    except Exception:  # noqa: BLE001 — a config-read error must never wedge the tool call.
        return True


def handle_block_foreign_mcp_write(data: dict) -> bool:
    tool_name = data.get("tool_name", "")
    if not is_foreign_mcp_write(tool_name):
        return False
    if is_slack_mcp_tool(tool_name) and not _slack_gate_enabled():
        return False
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 deferred back-import

    reason = next((message for keyword, message in _REDIRECTS if keyword in tool_name.lower()), _OTHER_DENY_REASON)
    return _fail_open_or_deny(data, reason, gate_id="mcp_foreign_write")
