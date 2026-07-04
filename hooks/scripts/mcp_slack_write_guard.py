"""Deny a direct MCP Slack WRITE — every Slack write goes through the ``t3`` CLI (#1196).

A direct ``mcp__*slack*`` write tool (post / reply / reaction / update / delete /
upload) bypasses teatree's Slack egress chokepoint (``src/teatree/backends/slack/``
under the on-behalf gate, the voice classifier, the verify-by-re-read contract),
so a message can land under the user's identity with none of those guarantees.
This gate closes that bypass at the ``PreToolUse`` boundary: a Slack MCP WRITE is
denied and redirected to the sanctioned CLI; a Slack MCP READ (history / list /
search / get) passes through untouched.

Narrower and complementary to ``handle_block_self_dm_via_mcp`` (which refuses only
a self-DM write, fail-closed on unreadable config): this gate refuses EVERY Slack
MCP write regardless of destination, so the direct-MCP path is closed wholesale.
The two coexist — whichever fires first in the chain emits the deny.

Cold-import safe: the live ``PreToolUse`` hook is a bare ``python3`` subprocess
with no guarantee ``teatree`` is importable, so the module top imports only stdlib.
The deny writer (``emit_pretooluse_deny``) and the kill-switch reader
(``_teatree_bool_setting``) are back-imported lazily inside the handler.
"""

import re
import sys

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# re-exports and a test patching a helper here operate on ONE module object —
# the pattern every bare sibling (``raw_review_post_guard`` …) uses.
sys.modules.setdefault("mcp_slack_write_guard", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.mcp_slack_write_guard", sys.modules[__name__])

#: Write-verb fragments a Slack MCP write tool's suffix carries. Conservative by
#: construction — a read tool (``get_channel_history``, ``list_channels``,
#: ``search_messages``, ``get_users``) carries none of these, so it passes.
_SLACK_WRITE_VERBS: frozenset[str] = frozenset(
    {
        "send",
        "post",
        "reply",
        "reaction",
        "react",
        "update",
        "delete",
        "upload",
        "schedule",
        "write",
    }
)

#: Per-call never-lockout escape: ``[slack-mcp-ok: <reason>]`` in any string field
#: of the tool input allows that single call (a vetted one-off). Empty reason rejects.
_ESCAPE_RE = re.compile(r"\[slack-mcp-ok:\s*(\S[^\]]*)\]")

_DENY_REASON = (
    "BLOCKED: a direct MCP Slack write bypasses teatree's Slack egress chokepoint "
    "(on-behalf gate, voice classifier, verify-by-re-read). Route it through the "
    "`t3` CLI instead: DM the user with `t3 teatree notify send -` (bot token); post to "
    "a colleague channel with `t3 <overlay> notify post --channel <id> --text <body>` "
    "(on-behalf gated); react with `t3 slack react`; comment on an MR/PR with "
    "`t3 <overlay> review post-comment`. Slack MCP READS (history/list/search/get) are "
    "unaffected. One-off escape: put `[slack-mcp-ok: <reason>]` in the message text."
)


def is_slack_mcp_tool(tool_name: str) -> bool:
    """Whether *tool_name* is any Slack MCP tool (``mcp__*slack*``)."""
    return tool_name.startswith("mcp__") and "slack" in tool_name.lower()


def is_slack_mcp_write(tool_name: str) -> bool:
    """Whether *tool_name* is a Slack MCP WRITE (a write verb in its suffix)."""
    if not is_slack_mcp_tool(tool_name):
        return False
    suffix = tool_name.rsplit("__", 1)[-1].lower()
    return any(verb in suffix for verb in _SLACK_WRITE_VERBS)


def _has_escape_token(tool_input: dict) -> bool:
    """Whether any string field of *tool_input* carries a valid ``[slack-mcp-ok: …]`` token."""
    return any(isinstance(value, str) and _ESCAPE_RE.search(value) for value in tool_input.values())


def _gate_enabled() -> bool:
    """Whether the gate is enabled (default True); a broken config fails OPEN to enabled."""
    try:
        from hook_router import _teatree_bool_setting  # noqa: PLC0415, PLC2701

        return _teatree_bool_setting("mcp_slack_write_gate_enabled", default=True)
    except Exception:  # noqa: BLE001 — a config-read error must never wedge the tool call.
        return True


def handle_block_mcp_slack_write(data: dict) -> bool:
    """Deny a direct MCP Slack WRITE, redirecting to the sanctioned ``t3`` CLI.

    Fires on any ``mcp__*slack*`` tool whose suffix carries a write verb; a Slack
    READ tool passes through. Never-lockout: the ``[teatree]
    mcp_slack_write_gate_enabled = false`` kill-switch disables it, and a
    ``[slack-mcp-ok: <reason>]`` token in the tool input allows a single call.
    Returns True when a deny was emitted (caller stops the handler chain).
    """
    if not _gate_enabled():
        return False
    if not is_slack_mcp_write(data.get("tool_name", "")):
        return False
    tool_input = data.get("tool_input", {}) or {}
    if isinstance(tool_input, dict) and _has_escape_token(tool_input):
        return False
    from hook_router import emit_pretooluse_deny  # noqa: PLC0415

    return emit_pretooluse_deny(_DENY_REASON)
