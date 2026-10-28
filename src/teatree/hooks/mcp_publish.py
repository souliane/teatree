"""The text an MCP write tool publishes: Slack's explicit roster, and any Notion connector write.

A Notion connector tool counts as a read only when its name carries a read verb, so a tool this
module has never seen is a write and its text is scanned. The Slack roster is explicit, because a
Slack MCP write is refused outright elsewhere and only its body fields need naming.
"""

import re
from collections.abc import Mapping
from typing import Final

# An explicit roster: a substring "send" heuristic missed schedule/canvas tools.
_SLACK_MCP_WRITE_TOOLS: Final[dict[str, tuple[str, ...]]] = {
    "slack_send_message": ("text", "message"),
    "slack_send_message_draft": ("text", "message"),
    "slack_schedule_message": ("text", "message"),
    "slack_edit_message": ("text", "message"),
    "slack_create_canvas": ("document_content", "content", "text"),
    "slack_update_canvas": ("document_content", "content", "text"),
    "slack_create_conversation": ("name",),
}

_NOTION_READ_VERBS: Final[frozenset[str]] = frozenset(
    {"fetch", "get", "list", "query", "read", "retrieve", "search", "view"}
)
_NOTION_FETCH_VERBS: Final[frozenset[str]] = frozenset({"fetch", "read", "retrieve", "view"})
_NOTION_WRITE_VERBS: Final[frozenset[str]] = frozenset(
    {"append", "archive", "create", "delete", "duplicate", "edit", "move", "patch", "replace", "send", "update"}
)
_NOTION_IDENTITY_KEYS: Final[frozenset[str]] = frozenset(
    {"id", "page_id", "block_id", "database_id", "data_source_id", "parent_id", "discussion_id", "user_id"}
)
_NAME_WORD_RE: Final = re.compile(r"[A-Z]?[a-z]+")
_NOTION_OBJECT_ID_RE: Final = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])"
)


def _tool_words(tool_name: str) -> frozenset[str]:
    suffix = tool_name.rsplit("__", 1)[-1]
    return frozenset(word.lower() for word in _NAME_WORD_RE.findall(suffix))


def is_notion_mcp_tool(tool_name: str) -> bool:
    return tool_name.startswith("mcp__") and "notion" in tool_name.lower()


def is_notion_mcp_write(tool_name: str) -> bool:
    words = _tool_words(tool_name)
    return is_notion_mcp_tool(tool_name) and (bool(words & _NOTION_WRITE_VERBS) or not words & _NOTION_READ_VERBS)


def is_notion_mcp_fetch(tool_name: str) -> bool:
    return (
        is_notion_mcp_tool(tool_name)
        and not is_notion_mcp_write(tool_name)
        and bool(_tool_words(tool_name) & _NOTION_FETCH_VERBS)
    )


def text_leaves(value: object, *, skip_keys: frozenset[str] = frozenset()) -> list[str]:
    """Every non-empty string inside *value*, in reading order, without recursing."""
    leaves: list[str] = []
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if item:
                leaves.append(item)
        elif isinstance(item, Mapping):
            pending.extend(child for key, child in reversed(list(item.items())) if key not in skip_keys)
        elif isinstance(item, list):
            pending.extend(reversed(item))
    return leaves


def values_under_keys(value: object, keys: frozenset[str]) -> list[str]:
    """Every string stored under one of *keys*, at any depth."""
    found: list[str] = []
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, Mapping):
            for key, child in item.items():
                if key in keys and isinstance(child, str):
                    found.append(child)
                else:
                    pending.append(child)
        elif isinstance(item, list):
            pending.extend(item)
    return found


def notion_write_text(tool_name: str, tool_input: Mapping[str, object]) -> str | None:
    """The prose a Notion write publishes (``""`` when it carries none), or ``None`` for anything else."""
    if not is_notion_mcp_write(tool_name):
        return None
    leaves: list[str] = []
    pending: list[tuple[str, object, bool]] = [("", tool_input, False)]
    while pending:
        key, item, in_content_updates = pending.pop()
        if isinstance(item, str):
            if item and not (key in _NOTION_IDENTITY_KEYS and _NOTION_OBJECT_ID_RE.search(item)):
                leaves.append(item)
        elif isinstance(item, Mapping):
            pending.extend(
                (name, child, in_content_updates or name == "content_updates")
                for name, child in reversed(list(item.items()))
                if not (in_content_updates and name == "old_str")
            )
        elif isinstance(item, list):
            pending.extend((key, child, in_content_updates) for child in reversed(item))
    return "\n".join(leaves)


def _extract_slack_mcp_payload(tool_name: str, tool_input: Mapping[str, object]) -> str | None:
    if not (tool_name.startswith("mcp__") and "slack" in tool_name.lower()):
        return None
    fields = _SLACK_MCP_WRITE_TOOLS.get(tool_name.rsplit("__", 1)[-1].lower())
    if fields is None:
        return None
    for field_name in fields:
        value = tool_input.get(field_name)
        if isinstance(value, str) and value:
            return value
    return ""


def extract_mcp_publish_payload(tool_name: str, tool_input: Mapping[str, object]) -> str | None:
    """The text-to-scan for an MCP write tool, or ``None`` when the call publishes nothing."""
    notion = notion_write_text(tool_name, tool_input)
    return notion if notion is not None else _extract_slack_mcp_payload(tool_name, tool_input)
