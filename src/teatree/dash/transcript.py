"""Opt-in redacted transcript tail for the ticket drawer (#3673 Tier 2).

A ``TaskAttempt.agent_session_id`` resolves to a Claude transcript on disk
(``~/.claude/projects/<project>/<session_id>.jsonl`` — the same layout
:mod:`teatree.claude_sessions` reads). This module tails a BOUNDED number of the
most-recent lines (a large transcript is never loaded whole into a template) and
redacts every extracted line through the shared leak-gate policy
(:func:`teatree.core.gates.privacy_gate.redact_for_local_display`) before it
reaches the view. It is a click-through only — nothing here runs during list
rendering.
"""

import json
import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

from teatree.core.gates.privacy_gate import redact_for_local_display

logger = logging.getLogger(__name__)

#: Most-recent lines kept from a transcript — the bound that keeps a large file
#: from ever reaching a template. A tail, not the whole conversation.
TAIL_LINES = 200

#: Per-line character cap after redaction — a single tool-result line can be huge.
_LINE_CHARS = 2000


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    """One redacted, truncated transcript line ready to render."""

    role: str
    text: str


class TranscriptLine(TypedDict, total=False):
    """The raw ``json.loads`` shape the tailer reads, across all three nesting levels.

    Every value arrives from untyped JSON — nothing is guaranteed present
    (``total=False``) and each read is isinstance-guarded at the call site. One
    shape covers the top-level entry (``type`` / ``message`` / ``content``), the
    nested ``message`` (``content``), and a content block (``type`` / ``text`` /
    ``name``).
    """

    type: str
    message: "TranscriptLine"
    content: object
    text: str
    name: str


def _projects_dir(projects_dir: Path | None) -> Path:
    return projects_dir if projects_dir is not None else Path.home() / ".claude" / "projects"


def transcript_path(session_id: str, *, projects_dir: Path | None = None) -> Path | None:
    """The ``<session_id>.jsonl`` under any project dir, or ``None`` when absent.

    Both lanes (headless and interactive) key their transcript by session id, so
    a single glob across every project dir resolves either.
    """
    if not session_id:
        return None
    root = _projects_dir(projects_dir)
    if not root.is_dir():
        return None
    for candidate in root.glob(f"*/{session_id}.jsonl"):
        return candidate
    return None


def tail_transcript(
    session_id: str,
    *,
    projects_dir: Path | None = None,
    lines: int = TAIL_LINES,
) -> list[TranscriptEntry]:
    """The last *lines* transcript entries, each redacted and truncated.

    Streams the file through a bounded ``deque`` so only *lines* lines are ever
    held in memory — the whole transcript is never materialised. Fails open to an
    empty list: a missing/unreadable transcript yields no rows, never a raise.
    """
    path = transcript_path(session_id, projects_dir=projects_dir)
    if path is None:
        return []
    try:
        with path.open(encoding="utf-8") as handle:
            recent = deque(handle, maxlen=max(1, lines))
    except OSError:
        logger.warning("transcript tail read failed for session %r", session_id, exc_info=True)
        return []
    rows: list[TranscriptEntry] = []
    for raw in recent:
        entry = _parse_line(raw)
        if entry is not None:
            rows.append(entry)
    return rows


def _parse_line(raw: str) -> TranscriptEntry | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    entry = cast("TranscriptLine", obj)
    text = _extract_text(entry)
    if not text:
        return None
    redacted = redact_for_local_display(text)[:_LINE_CHARS]
    return TranscriptEntry(role=str(entry.get("type", "")), text=redacted)


def _extract_text(obj: TranscriptLine) -> str:
    """A readable one-line preview of a transcript entry's payload.

    Message text is pulled from the nested ``message.content`` (string or the
    content-block list); a non-message entry falls back to its own ``content``.
    Tool payloads collapse to a short preview rather than a raw dump.
    """
    message = obj.get("message")
    if isinstance(message, dict):
        return _content_preview(cast("TranscriptLine", message).get("content"))
    return _content_preview(obj.get("content"))


def _content_preview(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for raw in content:
        if not isinstance(raw, dict):
            continue
        block = cast("TranscriptLine", raw)
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text.strip())
        elif kind == "tool_use":
            name = block.get("name")
            if isinstance(name, str):
                parts.append(f"[tool_use: {name}]")
        elif kind == "tool_result":
            parts.append("[tool_result]")
    return " ".join(p for p in parts if p)
