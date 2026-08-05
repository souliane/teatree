"""Transcript turn-inspection helpers shared by Stop gates.

Each reads the most recent assistant turn and returns one projection of it:
``current_turn_tool_commands`` flattens every ``tool_use`` input string (the
closure-reverify Stop gate, #1448), ``current_turn_edits`` returns the edited file
paths, and ``current_turn_assistant_text`` the assistant prose (both feed the
consideration Stop gate). Factored OUT of ``hook_router`` (a shrink-only capped
god-module): a new gate that needs the same walk imports it from here rather than
growing the router.

All three project over ONE turn-boundary walk (:func:`current_turn_assistant_blocks`)
that reads ``question_gates``' predicates for what an entry's role and blocks are
and for what counts as the user speaking. A tool RESULT is recorded as a ``user``
entry whose blocks are all ``tool_result``, so a walk that breaks at the first
``user`` entry cuts the turn at the first tool call — and every real tool-using
turn then projects to nothing. Keeping one walk with one boundary predicate is
what stops the three copies drifting apart the way this module drifted from
``question_gates.last_assistant_turn`` after the same fix landed there.
"""

import sys

from hooks.scripts.question_gates import (
    _entry_message_blocks,
    _entry_message_role,
    is_tool_result_only,
    read_transcript_entries,
)

# Alias the bare and ``hooks.scripts.`` identities so the router and any test
# patching a helper here operate on ONE module object.
sys.modules.setdefault("turn_inspect", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.turn_inspect", sys.modules[__name__])


def current_turn_assistant_blocks(transcript_path: str) -> list[dict]:
    """Every assistant content block of the most recent turn, in transcript order.

    Walks newest→oldest to the most recent GENUINE ``user`` entry. A ``user``
    entry whose blocks are all ``tool_result`` is the harness recording a tool's
    output, not the user typing, so it is walked past
    (:func:`question_gates.is_tool_result_only`) — breaking there would end the
    turn at its first tool call.
    """
    blocks: list[dict] = []
    for entry in reversed(read_transcript_entries(transcript_path)):
        role = _entry_message_role(entry)
        if role == "user":
            if is_tool_result_only(_entry_message_blocks(entry)):
                continue
            break
        if role != "assistant":
            continue
        blocks.extend(block for block in _entry_message_blocks(entry) if isinstance(block, dict))
    blocks.reverse()
    return blocks


#: ``tool_use`` input fields that can carry an id + state-read verb.
_COMMAND_FIELDS: tuple[str, ...] = ("command", "prompt", "description")


def current_turn_tool_commands(transcript_path: str) -> list[str]:
    """Flattened text of every tool_use input in the most recent turn.

    Collects, for each ``tool_use`` block of the turn, the strings that can carry
    an id + state-read verb: ``Bash`` ``command`` and ``Agent`` / ``Task``
    ``prompt`` + ``description``. These feed the same-turn-verification check so a
    ``gh pr view <id>`` in the turn clears the warning for that id.
    """
    commands: list[str] = []
    for block in current_turn_assistant_blocks(transcript_path):
        if block.get("type") != "tool_use":
            continue
        tool_input = block.get("input")
        if not isinstance(tool_input, dict):
            continue
        for field in _COMMAND_FIELDS:
            value = tool_input.get(field)
            if isinstance(value, str) and value:
                commands.append(value)
    return commands


_EDIT_TOOL_NAMES = frozenset({"Edit", "Write", "NotebookEdit"})


def _edit_block_path(block: dict) -> str | None:
    """File path for an ``Edit``/``Write``/``NotebookEdit`` tool_use block.

    Caller pre-filters with ``isinstance(block, dict)`` (mirrors the
    ``_block_is_settings_write`` contract).
    """
    if block.get("type") != "tool_use":
        return None
    name = block.get("name")
    if name not in _EDIT_TOOL_NAMES:
        return None
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        return None
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if isinstance(raw, str) and raw:
        return raw
    return None


def current_turn_edits(transcript_path: str) -> list[str]:
    """File paths edited by the assistant in the most recent turn, in transcript order.

    Duplicates kept — the caller classifies + dedupes.
    """
    paths = (_edit_block_path(block) for block in current_turn_assistant_blocks(transcript_path))
    return [path for path in paths if path is not None]


def current_turn_assistant_text(transcript_path: str) -> str:
    """Concatenated assistant text blocks in the most recent turn.

    Used to detect a teatree-issue reference that clears the gate.
    """
    chunks: list[str] = []
    for block in current_turn_assistant_blocks(transcript_path):
        text = block.get("text")
        if block.get("type") == "text" and isinstance(text, str):
            chunks.append(text)
    return "\n".join(chunks)
