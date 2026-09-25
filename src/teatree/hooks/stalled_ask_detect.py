"""Detect a transcript's trailing, still-blocking ``AskUserQuestion`` (#4818).

Shared leaf: both the ``Notification`` hook (``hooks/scripts/notification_dm_gate.py``,
imported lazily via the ``teatree_src_on_path`` ``src/`` bootstrap since the hook
interpreter is a bare ``python3`` with no guarantee ``teatree`` is importable) and the
``t3 doctor check`` stalled-ask finding (``teatree.cli.doctor.checks_session``, a normal
package import) need the SAME answer: is the transcript's LAST entry an
``AskUserQuestion`` tool_use with no ``tool_result`` after it yet — i.e. a call that is
genuinely still blocking on the owner? One leaf, so the Notification DM and the doctor
finding can never disagree about what counts as "still pending".

Pure JSON parsing, no subprocess and no ORM — unit-testable and safe to import from
either side.
"""

import json
from pathlib import Path


def pending_ask_text(transcript_path: str) -> str | None:
    """The question text of a trailing, still-blocking ``AskUserQuestion`` call.

    "Trailing" means the transcript's LAST entry is the assistant's own ``tool_use``
    block, with nothing after it — exactly the shape while the call genuinely blocks
    on the owner (a ``tool_result`` lands only once it returns). Fail-safe: any other
    shape (already answered, no ask posed, an unreadable/empty transcript) is ``None``.
    """
    last = _last_entry(transcript_path)
    if last is None:
        return None
    message = last.get("message") if isinstance(last, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list) or not content:
        return None
    block = content[-1]
    if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") != "AskUserQuestion":
        return None
    tool_input = block.get("input")
    questions = tool_input.get("questions", []) if isinstance(tool_input, dict) else []
    first = questions[0] if isinstance(questions, list) and questions else {}
    text = str(first.get("question", "")).strip() if isinstance(first, dict) else ""
    return text or None


def _last_entry(transcript_path: str) -> dict | None:
    """The last well-formed JSON object in *transcript_path*, or ``None``.

    Reads from the END of the file (lines split, walked in reverse) rather than
    parsing every entry — the only thing either caller ever needs is the trailing one.
    """
    if not transcript_path:
        return None
    path = Path(transcript_path)
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw_line in reversed(raw.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        return parsed if isinstance(parsed, dict) else None
    return None
