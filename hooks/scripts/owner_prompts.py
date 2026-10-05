"""What the owner typed and answered in this session, read newest-first from its transcript."""

import contextlib
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import islice
from pathlib import Path
from typing import Final

from hooks.scripts.loop_prompt_shape import is_bare_loop_prompt
from hooks.scripts.question_gates import (
    _entry_message_blocks,
    _entry_message_role,
    _parsed_entry,
    is_tool_result_only,
    iter_transcript_reversed,
)
from hooks.scripts.skill_loader_input import strip_ambient_context

sys.modules.setdefault("owner_prompts", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.owner_prompts", sys.modules[__name__])

_HUMAN_ORIGIN: Final[str] = "human"

#: Long enough to cover the tool calls between the owner acting and the agent asking;
#: short enough that a later autonomous turn is never mistaken for them.
LIVE_TURN_FRESHNESS: Final[timedelta] = timedelta(seconds=90)

#: A slash command reaches the transcript only inside these harness wrappers.
_COMMAND_NAME_RE: Final[re.Pattern[str]] = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_COMMAND_ARGS_RE: Final[re.Pattern[str]] = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)


def is_owner_prompt(entry: dict) -> bool:
    """Whether *entry* is the owner typing — not a scheduled fire, skill body, summary or relayed output."""
    origin = entry.get("origin")
    return (
        _entry_message_role(entry) == "user"
        and not entry.get("isMeta")
        and not entry.get("isCompactSummary")
        and entry.get("turnOrigin", _HUMAN_ORIGIN) == _HUMAN_ORIGIN
        and "scheduledFireId" not in entry
        and (not isinstance(origin, dict) or origin.get("kind", _HUMAN_ORIGIN) == _HUMAN_ORIGIN)
        and not is_tool_result_only(_entry_message_blocks(entry))
    )


def entry_text(entry: dict) -> str:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    return "\n".join(
        str(block.get("text", ""))
        for block in _entry_message_blocks(entry)
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _is_owner_words(entry: dict) -> bool:
    return is_owner_prompt(entry) and not is_bare_loop_prompt(entry_text(entry))


def _is_owner_answer(entry: dict) -> bool:
    result = entry.get("toolUseResult")
    return isinstance(result, dict) and bool(result.get("answers"))


def _acted_at(entry: dict) -> datetime | None:
    try:
        acted_at = datetime.fromisoformat(entry["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None
    return acted_at if acted_at.tzinfo is not None else None


def _owner_words(transcript_path: str) -> Iterator[dict]:
    return (entry for entry in iter_transcript_reversed(transcript_path) if _is_owner_words(entry))


def owner_messages(transcript_path: str, *, limit: int) -> list[str] | None:
    """The owner's newest *limit* messages, ambient blocks stripped; ``None`` when the transcript is unreadable."""
    if not transcript_path or not Path(transcript_path).is_file():
        return None
    return [strip_ambient_context(entry_text(entry)) for entry in islice(_owner_words(transcript_path), limit)]


def _as_typed(entry: dict) -> str:
    """The prompt as the owner typed it: a slash command's name and arguments unwrapped, any other text as is."""
    text = entry_text(entry)
    name = _COMMAND_NAME_RE.search(text)
    if name is None:
        return text
    args = _COMMAND_ARGS_RE.search(text)
    return " ".join(part for part in (name.group(1).strip(), args.group(1).strip() if args else "") if part)


#: How much of the range past a cursor one look reads, from its newest end: at most this many bytes, and
#: of them at most this many lines. A range past either (calls that went unread, a huge tool output) costs
#: the same as a short one and still moves the cursor to its end; the newest owner prompt is what matters.
LOOK_BYTES: Final[int] = 4 * 1024 * 1024
LOOK_LINES: Final[int] = 2_000


def _lines_newest_first(whole: bytes) -> Iterator[bytes]:
    """The lines of *whole* (empty, or ending in a newline) from the last one back, split as they are reached."""
    stop = len(whole) - 1
    while stop >= 0:
        begin = whole.rfind(b"\n", 0, stop) + 1
        yield whole[begin:stop]
        stop = begin - 1


@dataclass(frozen=True, slots=True)
class _Look:
    """The whole lines read past the cursor and the offset just past them; a first look reads from the start."""

    whole: bytes
    first_look: bool
    offset: int

    def newest_first(self) -> Iterator[dict]:
        lines = islice(_lines_newest_first(self.whole), LOOK_LINES)
        return (entry for raw in lines if raw.strip() and (entry := _parsed_entry(raw)) is not None)


def _look_since(cursor: Path, transcript_path: str) -> _Look | None:
    """The newest whole lines past the offset in *cursor*, at most :data:`LOOK_BYTES`, and the offset past them.

    No offset, or one outside the transcript (a rewritten one is shorter), is a first look from the start. A
    range cut by the bound starts at its first whole line, so a line is read only when it ends, newline
    included, within the last :data:`LOOK_BYTES` bytes. A full window (:data:`LOOK_BYTES` bytes) with no line
    end is therefore one line too wide to ever be read: the offset moves past it, so it is not re-read on every
    look; a shorter tail with no line end is a line still being written, and the offset waits for it. ``None``
    when the transcript cannot be read.
    """
    try:
        with Path(transcript_path).open("rb") as handle:
            end = handle.seek(0, 2)
            try:
                recorded = int(cursor.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                recorded = -1
            first_look = not 0 <= recorded <= end
            start = 0 if first_look else recorded
            # One byte before a cut tells whether the bound falls on a line's first byte.
            begin = start if end - start <= LOOK_BYTES else end - LOOK_BYTES - 1
            handle.seek(begin)
            read = handle.read(end - begin)
    except OSError:
        return None
    last = read.rfind(b"\n")
    if last < 0:
        return _Look(b"", first_look=first_look, offset=end if end - start >= LOOK_BYTES else start)
    first = 0 if end - start <= LOOK_BYTES else read.find(b"\n") + 1
    return _Look(read[first : last + 1], first_look=first_look, offset=begin + last + 1)


def _record(cursor: Path, offset: int) -> bool:
    try:
        cursor.write_text(str(offset), encoding="utf-8")
    except OSError:
        return False
    return True


def owner_prompted_since(cursor: Path, transcript_path: str) -> bool:
    """Whether the owner typed a prompt past the byte offset in *cursor*, which then moves to the last whole line.

    Reads only the newest end of what was appended since the previous look (:data:`LOOK_BYTES`,
    :data:`LOOK_LINES`); a first look records the last whole line and reports nothing.
    """
    look = _look_since(cursor, transcript_path)
    if look is None or not _record(cursor, look.offset):
        return False
    return not look.first_look and any(_is_owner_words(entry) for entry in look.newest_first())


@dataclass(frozen=True, slots=True)
class UnreadPrompts:
    """The owner's prompts not yet handed out, oldest first, as typed — read until :meth:`mark_read`."""

    prompts: tuple[str, ...]
    cursor: Path
    offset: int | None

    def mark_read(self) -> None:
        """Move the cursor past these prompts, once whatever they were for has been delivered."""
        if self.offset is not None:
            with contextlib.suppress(OSError):
                self.cursor.write_text(str(self.offset), encoding="utf-8")


def owner_prompts_since(cursor: Path, transcript_path: str) -> UnreadPrompts:
    """The owner's prompts past the byte offset in *cursor*; the cursor moves only when they are marked read.

    Only the newest end of the range is read (:data:`LOOK_BYTES`, :data:`LOOK_LINES`), back from its last
    line; a first look hands out the newest prompt already in the transcript.
    """
    look = _look_since(cursor, transcript_path)
    if look is None:
        return UnreadPrompts((), cursor, None)
    owner_words = (entry for entry in look.newest_first() if _is_owner_words(entry))
    newest = list(islice(owner_words, 1 if look.first_look else None))
    return UnreadPrompts(tuple(_as_typed(entry) for entry in reversed(newest)), cursor, look.offset)


def is_live_user_turn(transcript_path: str, *, now: datetime | None = None) -> bool:
    """Whether the owner typed a prompt, or answered a question in-client, within the live window."""
    for entry in iter_transcript_reversed(transcript_path):
        if _is_owner_words(entry) or _is_owner_answer(entry):
            acted_at = _acted_at(entry)
            return acted_at is not None and (now or datetime.now(tz=UTC)) - acted_at <= LIVE_TURN_FRESHNESS
    return False


__all__ = [
    "LIVE_TURN_FRESHNESS",
    "LOOK_BYTES",
    "LOOK_LINES",
    "UnreadPrompts",
    "entry_text",
    "is_live_user_turn",
    "is_owner_prompt",
    "owner_messages",
    "owner_prompted_since",
    "owner_prompts_since",
]
