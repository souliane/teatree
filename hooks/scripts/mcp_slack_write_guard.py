"""Deny a direct MCP Slack WRITE — every Slack write goes through the ``t3`` CLI (#1196).

A direct ``mcp__*slack*`` write tool (post / reply / reaction / update / delete /
upload) bypasses teatree's Slack egress chokepoint (``src/teatree/backends/slack/``
under the on-behalf gate, the voice classifier, the verify-by-re-read contract),
so a message can land under the user's identity with none of those guarantees.
This gate closes that bypass at the ``PreToolUse`` boundary: a Slack MCP WRITE is
denied and redirected to the sanctioned CLI; a recognised Slack MCP READ (history
/ list / search / get / info / …) passes through untouched. The classification is
default-DENY (:func:`is_slack_mcp_write`) — a tool whose shape the READ roster
does not recognise is a write, so a Slack MCP surface added after this roster
cannot slip through on a missing verb.

Narrower and complementary to ``handle_block_self_dm_via_mcp`` (which refuses only
a self-DM write, fail-closed on unreadable config): this gate refuses EVERY Slack
MCP write regardless of destination, so the direct-MCP path is closed wholesale.
The two coexist — whichever fires first in the chain emits the deny.

Cold-import safe: the live ``PreToolUse`` hook is a bare ``python3`` subprocess
with no guarantee ``teatree`` is importable, so the module top imports only stdlib.
Because the gate sits on the broad ``mcp__*slack*`` matcher, its deny routes
through the router's shared ``_fail_open_or_deny`` chokepoint (self-rescue
allowlist + master ``danger_gate_fail_open`` switch — the never-lockout contract);
that helper and the kill-switch reader (``_teatree_bool_setting``) are
back-imported lazily inside the handler.
"""

import re
import sys

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# re-exports and a test patching a helper here operate on ONE module object —
# the pattern every bare sibling (``raw_review_post_guard`` …) uses.
sys.modules.setdefault("mcp_slack_write_guard", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.mcp_slack_write_guard", sys.modules[__name__])

#: Verbs that make a Slack MCP tool a READ. Everything else — including a tool
#: whose name this roster has never seen — is treated as a WRITE, so a Slack MCP
#: surface the roster predates (``slack_create_conversation``,
#: ``slack_create_canvas``) fails CLOSED instead of passing on a missing write
#: verb. The inverse (a write-verb roster) also mis-classified the other way: a
#: pure read whose NOUN carries a write verb (``slack_get_reactions``) was denied
#: with no CLI equivalent to redirect it to.
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

#: A tool suffix's words, split on both ``_`` and camelCase (``conversations_list``
#: and ``conversationsHistory`` are the two spellings live Slack MCP servers use).
_NAME_WORD_RE = re.compile(r"[a-z]+|[A-Z][a-z]*")

#: Per-call never-lockout escape: ``[slack-mcp-ok: <reason>]`` in any string field
#: of the tool input allows that single call (a vetted one-off). Empty reason rejects.
_ESCAPE_RE = re.compile(r"\[slack-mcp-ok:\s*(\S[^\]]*)\]")

_DENY_REASON = (
    "BLOCKED: a direct MCP Slack write bypasses teatree's Slack egress chokepoint "
    "(on-behalf gate, voice classifier, verify-by-re-read). Route it through the "
    "`t3` CLI instead: DM the user with `t3 teatree notify send -` (bot token); post to "
    "a colleague channel with `t3 <overlay> notify post --channel <id> --text <body>` "
    "(on-behalf gated); react with `t3 slack react`; comment on an MR/PR with "
    "`t3 review post-comment`. Recognised Slack MCP READS "
    "(get/list/search/history/info/read/view/fetch/replies/members) are unaffected; a "
    "Slack MCP tool this gate does not recognise is treated as a WRITE, so a genuine "
    "read it has not seen lands here too. One-off escape: put `[slack-mcp-ok: <reason>]` "
    "in the message text."
)


def is_slack_mcp_tool(tool_name: str) -> bool:
    """Whether *tool_name* is any Slack MCP tool (``mcp__*slack*``)."""
    return tool_name.startswith("mcp__") and "slack" in tool_name.lower()


def is_slack_mcp_write(tool_name: str) -> bool:
    """Whether *tool_name* is a Slack MCP WRITE — i.e. anything not a recognised READ.

    Default-DENY: only a suffix carrying a :data:`_SLACK_READ_VERBS` word is a
    read. A Slack MCP tool this roster has never seen is a write, because the
    cost of the two errors is not symmetric — an unrecognised read is one denied
    call the operator escapes with ``[slack-mcp-ok: …]``, an unrecognised write
    is a post under the user's identity outside the egress chokepoint.
    """
    if not is_slack_mcp_tool(tool_name):
        return False
    suffix = tool_name.rsplit("__", 1)[-1]
    words = {word.lower() for word in _NAME_WORD_RE.findall(suffix)}
    return not (words & _SLACK_READ_VERBS)


def _has_escape_token(tool_input: dict) -> bool:
    """Whether any string field of *tool_input* carries a valid ``[slack-mcp-ok: …]`` token."""
    return any(isinstance(value, str) and _ESCAPE_RE.search(value) for value in tool_input.values())


def _gate_enabled() -> bool:
    """Whether the gate is enabled (default True); a broken config fails OPEN to enabled."""
    try:
        from hooks.scripts.hook_router import _teatree_bool_setting  # noqa: PLC0415 deferred back-import

        return _teatree_bool_setting("mcp_slack_write_gate_enabled", default=True)
    except Exception:  # noqa: BLE001 — a config-read error must never wedge the tool call.
        return True


def handle_block_mcp_slack_write(data: dict) -> bool:
    """Deny a direct MCP Slack WRITE, redirecting to the sanctioned ``t3`` CLI.

    Fires on any ``mcp__*slack*`` tool that is not a recognised READ; a Slack READ
    tool passes through. Never-lockout: the ``[teatree]
    mcp_slack_write_gate_enabled = false`` kill-switch disables it, a
    ``[slack-mcp-ok: <reason>]`` token in the tool input allows a single call, and
    the deny routes through the router's shared ``_fail_open_or_deny`` chokepoint
    so the always-allowed self-rescue commands and the master ``[teatree]
    danger_gate_fail_open`` kill-switch apply. Returns True when a deny was emitted
    (caller stops the handler chain).
    """
    if not _gate_enabled():
        return False
    if not is_slack_mcp_write(data.get("tool_name", "")):
        return False
    tool_input = data.get("tool_input", {}) or {}
    if isinstance(tool_input, dict) and _has_escape_token(tool_input):
        return False
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 deferred back-import

    return _fail_open_or_deny(data, _DENY_REASON)
