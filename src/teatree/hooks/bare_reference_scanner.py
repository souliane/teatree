r"""Pre-publish bare-reference link gate (#1530).

A reference (issue ``#1500``, MR ``!6301``, a raw Slack ``ts`` such as
``1716900000.123456``, or a bare forge/Notion/Slack URL) is unclickable
noise when it ships as a literal token. This module is the deterministic
detector that the two hook handlers in ``hooks/scripts/hook_router.py``
use to promote the prose-only "always a clickable link, never a bare id"
rule (``feedback_always_clickable_links_never_bare_ids.md``) to a gate —
sibling of the #1213 quote-scanner and #1415 banned-terms gates.

The module is pure detection. It reuses the SAME token-aware publish-
surface parser (``teatree.hooks._command_parser``) so a single parser
feeds every gate, then matches the extracted body against the bare-
reference catalogue.

A reference is FLAGGED only when it is NOT already wrapped in a markdown
link ``[..](..)``, an HTML/Slack mrkdwn link ``<url|..>``, or an autolink
``<url>``. Every such linked span is excised before token matching so a
linked reference cannot trip a bare-token pattern. The catalogue is
deliberately conservative to stay clear of the lockout direction: only
``#\d+`` / ``!\d+`` tokens and explicit ts/URL shapes count — plain
numbers (``5 PRs``, ``100GB``, ``line 42``, ``v1.2.3``) and hex shas are
never flagged.

The one exemption is the conventional trailing ``(#NNNN)`` / ``(!NNNN)``
parenthetical at the END of a PR/MR title or a git-commit subject (#1544):
the forge auto-links the ref there, it is the universal conventional-commit
and MR-title convention, and it is required by the MR-title-convention gate.
The exemption is narrow — a bare ref anywhere else (description bodies,
slack-send, t3-notify, mid-title text) stays flagged.

Fail-closed on an unparsable body via the shared
``FAIL_CLOSED_SENTINEL`` (same contract as the quote-scanner).
"""

import re
from typing import Final, TypedDict

from teatree.hooks._command_parser import extract_bash_payload as _extract_bash_payload
from teatree.hooks._command_parser import extract_title_fragments as _extract_title_fragments
from teatree.hooks._command_parser import is_fail_closed_sentinel as _is_fail_closed_sentinel
from teatree.hooks._command_parser import is_publish_command as _is_publish_command


class ToolInput(TypedDict, total=False):
    command: str
    text: str
    message: str
    body: str
    document_content: str
    content: str
    env: dict[str, str]


# Linked spans excised before bare-token matching. A reference living
# inside any of these is already clickable and must not be flagged.
_MARKDOWN_LINK_RE: Final[re.Pattern[str]] = re.compile(r"\[[^\]]*\]\([^)]*\)")
_ANGLE_LINK_RE: Final[re.Pattern[str]] = re.compile(r"<[^>\s]+(?:\|[^>]*)?>")


_BARE_ISSUE_RE: Final[re.Pattern[str]] = re.compile(r"(?<![\w/])([#!]\d+)\b")
_BARE_SLACK_TS_RE: Final[re.Pattern[str]] = re.compile(r"(?<![\w.])(\d{10}\.\d{6})(?![\w.])")
_BARE_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"https?://(?:[\w.-]+\.)?(?:github\.com|gitlab\.com|notion\.so|notion\.site|slack\.com)/\S+",
)


_SLACK_MCP_WRITE_FIELDS: Final[tuple[str, ...]] = ("text", "message", "document_content", "content")


# The conventional trailing ``(#NNNN)`` / ``(!NNNN)`` parenthetical(s) at
# the END of a PR/MR title or git-commit subject (#1544). GitHub/GitLab
# auto-link the ref there and the suffix is the universal conventional-
# commit + MR-title convention, so it is exempt — but ONLY in that trailing
# position. Squash-merge appends its own ``(#NNNN)`` to an already-suffixed
# subject, so the match is greedy over consecutive trailing groups. ``$``
# anchors to the fragment end; trailing whitespace is tolerated.
_TRAILING_CONVENTIONAL_REF_RE: Final[re.Pattern[str]] = re.compile(r"(\s*\([#!]\d+\)\s*)+$")


def _strip_linked_spans(text: str) -> str:
    return _ANGLE_LINK_RE.sub(" ", _MARKDOWN_LINK_RE.sub(" ", text))


def find_bare_references(text: str) -> list[str]:
    if not text:
        return []
    unlinked = _strip_linked_spans(text)
    refs: list[str] = []
    seen: set[str] = set()
    for pattern in (_BARE_URL_RE, _BARE_ISSUE_RE, _BARE_SLACK_TS_RE):
        for match in pattern.finditer(unlinked):
            ref = match.group(0).rstrip(".,;:")
            if ref not in seen:
                seen.add(ref)
                refs.append(ref)
    return refs


def scan_text(text: str) -> list[str]:
    if not text:
        return []
    if _is_fail_closed_sentinel(text):
        return ["<unresolved publish body>"]
    return find_bare_references(text)


def _slack_tool_suffix(tool_name: str) -> str:
    return tool_name.rsplit("__", 1)[-1].lower()


def _extract_slack_mcp_payload(tool_name: str, tool_input: ToolInput) -> str | None:
    if not (tool_name.startswith("mcp__") and "slack" in tool_name.lower()):
        return None
    suffix = _slack_tool_suffix(tool_name)
    if not (suffix.startswith("slack_send") or suffix == "slack_schedule_message" or suffix.endswith("canvas")):
        return None
    for field_name in _SLACK_MCP_WRITE_FIELDS:
        value = tool_input.get(field_name)
        if isinstance(value, str) and value:
            return value
    return ""


def _exempt_trailing_title_suffix(payload: str, title_fragments: list[str]) -> str:
    """Drop the conventional trailing ``(#NNNN)`` suffix of each title fragment.

    Only the trailing parenthetical of a PR/MR title or git-commit subject
    is exempt (#1544). Each title fragment is its own line in the flattened
    payload, so the suffix is stripped from the matching whole line — never
    from a body line that merely embeds the title text as a substring. A
    body reference or a mid-title reference survives the scan.
    """
    pending = [f for f in title_fragments if _TRAILING_CONVENTIONAL_REF_RE.search(f)]
    if not pending:
        return payload
    lines = payload.split("\n")
    for index, line in enumerate(lines):
        if line in pending:
            lines[index] = _TRAILING_CONVENTIONAL_REF_RE.sub("", line)
            pending.remove(line)
    return "\n".join(lines)


def extract_publish_payload(tool_name: str, tool_input: ToolInput) -> str | None:
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if not _is_publish_command(command):
            return None
        payload = _extract_bash_payload(command)
        return _exempt_trailing_title_suffix(payload, _extract_title_fragments(command))
    return _extract_slack_mcp_payload(tool_name, tool_input)


def format_block_message(refs: list[str]) -> str:
    listed = ", ".join(refs)
    return (
        "BLOCKED: bare-reference link gate (#1530). The body cites bare reference(s) "
        f"{listed} instead of a clickable link. Render each as a clickable link, e.g. "
        "[#1500](https://github.com/owner/repo/issues/1500), before publishing."
    )


def format_warn_message(refs: list[str]) -> str:
    listed = ", ".join(refs)
    return (
        f"WARNING: bare-reference link gate (#1530) — the reply cited bare reference(s) {listed} "
        "instead of a clickable link. Next time render each as [#NNNN](url) so it is clickable."
    )
