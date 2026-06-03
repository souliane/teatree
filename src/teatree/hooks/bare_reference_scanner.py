r"""Pre-publish bare-reference link gate (#1530).

A bare reference (issue ``#1500``, MR ``!6301``, a raw Slack ``ts`` such
as ``1716900000.123456``) is unclickable noise when it ships to a
USER-FACING surface as a literal token. This module is the deterministic
detector that the two hook handlers in ``hooks/scripts/hook_router.py``
use to promote the prose-only "always a clickable link, never a bare id"
rule (``feedback_always_clickable_links_never_bare_ids.md``) to a gate —
sibling of the #1213 quote-scanner and #1415 banned-terms gates.

The gate is DESTINATION-AWARE (#1530). The clickable-link rule applies
ONLY to user-facing surfaces (a Slack DM to the operator, ``t3 notify
send``, the assistant's own chat). On an EXTERNAL FORGE surface
(``gh pr create``, ``gh issue comment``, ``glab mr create``/``note``, a
forge ``api`` write) GitHub/GitLab auto-render a bare ``#1764`` / ``!7546``
as a live cross-reference, so the bare id is PREFERRED there and the gate
must NOT fire — both bare ids and bare URLs are allowed. The
destination-kind split lives in
:mod:`teatree.hooks.publish_destination_kind`; this module's detector is
what the gate runs once the destination is known user-facing.

A bare URL is ALWAYS allowed, on every surface: a forge/Notion/Slack URL
is already clickable in Slack and the terminal, so rewriting it into
markdown is gratuitous churn (the prior over-block this fix removes). Only
the bare ``#N`` / ``!N`` / Slack ``ts`` tokens are user-facing noise.

The module is pure detection. It reuses the SAME token-aware publish-
surface parser (``teatree.hooks._command_parser``) so a single parser
feeds every gate, then matches the extracted body against the bare-
reference catalogue.

A reference is FLAGGED only when it is NOT already wrapped in a markdown
link ``[..](..)``, an HTML/Slack mrkdwn link ``<url|..>``, or an autolink
``<url>``, and NOT inside a VERBATIM external block (a fenced code block
or a ``>`` blockquote). Every such linked or verbatim span is excised
before token matching so a linked reference cannot trip a bare-token
pattern and a reproduced PR/MR description or quoted external comment
keeps its source's id form. The catalogue is deliberately conservative to
stay clear of the lockout direction: only ``#\d+`` / ``!\d+`` tokens and
explicit ts shapes count — plain numbers (``5 PRs``, ``100GB``,
``line 42``, ``v1.2.3``), bare URLs, and hex shas are never flagged.

Three exemptions apply:

Exemption 0 — verbatim external blocks: a bare ref inside a fenced code
block (triple-backtick) or a ``>`` blockquote is reproduced external
text; the source's id form is preferred when quoting it, so refs inside
such a span are excised before matching. This is the cleanest verbatim
detection the gate can do without a full Markdown parser.

Exemption 1 — trailing title suffix: the conventional trailing
``(#NNNN)`` / ``(!NNNN)`` parenthetical at the END of a PR/MR title or
a git-commit subject (#1544). The forge auto-links the ref there; it is
the universal conventional-commit and MR-title convention and is required
by the MR-title-convention gate. The exemption is narrow — a bare ref
anywhere else (description bodies, slack-send, t3-notify, mid-title
text) stays flagged.

Exemption 2 — body close trailers: a line that STARTS with a recognised
close/relates keyword (``Closes``, ``Fixes``, ``Resolves``, ``Refs``,
``Relates-to``, and their variants) followed by ``#N`` or ``!N`` is the
canonical AGENTS.md convention for auto-closing issues on merge (#1619).
The platform auto-links these keywords natively; requiring a markdown
link defeats the auto-close mechanism. The exemption is anchored to
``^`` (MULTILINE) + the keyword so a mid-sentence bare ref cannot be
smuggled past the gate by prefixing prose with a close keyword.

Fail-closed on an unparsable body via the shared
``FAIL_CLOSED_SENTINEL`` (same contract as the quote-scanner).
"""

import re
from pathlib import Path
from typing import Final, TypedDict

from teatree.hooks._command_parser import extract_bash_payload as _extract_bash_payload
from teatree.hooks._command_parser import extract_title_fragments as _extract_title_fragments
from teatree.hooks._command_parser import is_fail_closed_sentinel as _is_fail_closed_sentinel
from teatree.hooks._command_parser import is_publish_command as _is_publish_command
from teatree.hooks.publish_destination_kind import DestinationKind, classify_bash_destination


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

# Verbatim external blocks (fenced span / ``>`` blockquote) are reproduced
# source content — excised before matching so a bare ref inside is exempt (0).
_FENCED_BLOCK_RE: Final[re.Pattern[str]] = re.compile(r"```.*?```", re.DOTALL)
_BLOCKQUOTE_LINE_RE: Final[re.Pattern[str]] = re.compile(r"^\s*>.*$", re.MULTILINE)


_BARE_ISSUE_RE: Final[re.Pattern[str]] = re.compile(r"(?<![\w/])([#!]\d+)\b")
_BARE_SLACK_TS_RE: Final[re.Pattern[str]] = re.compile(r"(?<![\w.])(\d{10}\.\d{6})(?![\w.])")
# A bare forge/Notion/Slack URL is always allowed (already clickable), so it
# is not detected here — retained only as an export for ``core.review_findings``.
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


# Leading auto-close / relates trailers in body content (#1619). A line
# that starts with a recognised close/relates keyword followed by ``#N``
# or ``!N`` is the AGENTS.md canonical auto-close convention; the platform
# natively auto-links these trailers and requiring a markdown link defeats
# the mechanism. The match is anchored to ``^`` (MULTILINE) so only a
# genuine line-start trailer is exempt — a mid-sentence bare ref cannot
# be smuggled past the gate by prefixing prose with a close keyword.
#
# Keyword set extends ``teatree.core.close_trailer_scanner.CLOSE_TRAILER_RE``
# with ``refs?`` and ``relates?(?:\s*-\s*to|\s+to)?`` and adds ``!N``
# (MR reference) beside the existing ``#N`` / URL forms.
_BODY_CLOSE_TRAILER_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs?|relates?(?:\s*-\s*to|\s+to)?)(?:\s+part\s+of)?"
    r"(?::\s*|\s+)"
    r"(?:(?:[\w./-]+)?[#!]\d+|https?://\S+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_linked_spans(text: str) -> str:
    return _ANGLE_LINK_RE.sub(" ", _MARKDOWN_LINK_RE.sub(" ", text))


def _strip_verbatim_blocks(text: str) -> str:
    """Excise fenced code blocks and ``>`` blockquotes before bare-ref matching.

    Replaces each verbatim span with a space so a bare ref reproduced from
    external content (a quoted PR/MR description, a pasted comment) keeps
    the source's id form (exemption 0). Spacing keeps character offsets
    from merging adjacent tokens into a spurious new pattern.
    """
    return _BLOCKQUOTE_LINE_RE.sub(" ", _FENCED_BLOCK_RE.sub(" ", text))


def _strip_body_close_trailers(text: str) -> str:
    """Excise leading auto-close / relates trailer lines before bare-ref matching.

    Replaces each matching line with a space so character offsets stay
    stable and adjacent tokens cannot accidentally merge into a new pattern.
    """
    return _BODY_CLOSE_TRAILER_RE.sub(" ", text)


def find_bare_references(text: str) -> list[str]:
    if not text:
        return []
    unlinked = _strip_linked_spans(text)
    unlinked = _strip_verbatim_blocks(unlinked)
    unlinked = _strip_body_close_trailers(unlinked)
    refs: list[str] = []
    seen: set[str] = set()
    for pattern in (_BARE_ISSUE_RE, _BARE_SLACK_TS_RE):
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


def extract_publish_payload(tool_name: str, tool_input: ToolInput, cwd: Path | None = None) -> str | None:
    """Return the body the bare-reference gate should scan, or ``None`` to skip.

    A Bash command bound for an EXTERNAL FORGE (a ``gh``/``glab`` post, a
    forge ``api`` write, a t3 forge wrapper) returns ``None``: the forge
    auto-links bare ids, so the gate must NOT fire (#1530). A user-facing
    Bash publish returns its flattened body for scanning; a non-publish
    command returns ``None``. A Slack MCP tool is always user-facing.

    ``cwd`` is the harness-provided working directory; it is the fallback base
    for resolving a ``git commit -F <relpath>`` body file when the command
    names no commit dir of its own, so a relative body file unreadable from
    the cold hook's reset cwd is still scanned (the same root cause as #1415).
    """
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if not _is_publish_command(command):
            return None
        if classify_bash_destination(command) is DestinationKind.EXTERNAL_FORGE:
            return None
        payload = _extract_bash_payload(command, fail_closed_body_file=True, cwd=cwd)
        return _exempt_trailing_title_suffix(payload, _extract_title_fragments(command))
    return _extract_slack_mcp_payload(tool_name, tool_input)


def format_block_message(refs: list[str]) -> str:
    listed = ", ".join(refs)
    return (
        "BLOCKED: bare-reference link gate (#1530). This user-facing message cites bare "
        f"reference(s) {listed} that are not clickable for the operator. Render each as a "
        "clickable link, e.g. [#1500](https://github.com/owner/repo/issues/1500), or paste "
        "the plain URL (a bare URL is already clickable). Bare ids are fine on a forge "
        "post (gh/glab) — only user-facing surfaces need the link."
    )


def format_warn_message(refs: list[str]) -> str:
    listed = ", ".join(refs)
    return (
        f"WARNING: bare-reference link gate (#1530) — the reply cited bare reference(s) {listed} "
        "that are not clickable for the operator. Next time render each as [#NNNN](url), or paste "
        "the plain URL, so it is clickable."
    )
