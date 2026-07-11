"""Transcript turn-inspection helper shared by Stop gates.

``current_turn_tool_commands`` flattens every ``tool_use`` input string in the
most recent assistant turn — the closure-reverify Stop gate (#1448) feeds it the
same-turn state-check detection. Factored OUT of ``hook_router`` (a shrink-only
capped god-module): a new gate that needs the same walk imports it from here
rather than growing the router.

The transcript readers (``_read_transcript_entries`` / ``_entry_role`` /
``_entry_content``) live in ``hook_router`` and are imported lazily at call time
— ``hook_router`` imports this module at top level, so importing it back at top
level here would be a cycle.
"""

import sys

# Alias the bare and ``hooks.scripts.`` identities so the router and any test
# patching a helper here operate on ONE module object.
sys.modules.setdefault("turn_inspect", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.turn_inspect", sys.modules[__name__])


def current_turn_tool_commands(transcript_path: str) -> list[str]:
    """Flattened text of every tool_use input in the most recent turn.

    Walks the transcript newest->oldest to the most recent ``user`` boundary
    and collects, for each ``tool_use`` block after it, the strings that can
    carry an id + state-read verb: ``Bash`` ``command`` and ``Agent`` / ``Task``
    ``prompt`` + ``description``. These feed the same-turn-verification check
    so a ``gh pr view <id>`` in the turn clears the warning for that id.
    """
    from hooks.scripts.hook_router import (  # noqa: PLC0415 deferred back-import
        _entry_content,
        _entry_role,
        _read_transcript_entries,
    )

    entries = _read_transcript_entries(transcript_path)
    if not entries:
        return []
    commands: list[str] = []
    for entry in reversed(entries):
        role = _entry_role(entry)
        if role == "user":
            break
        if role != "assistant":
            continue
        for block in _entry_content(entry):
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_input = block.get("input")
            if not isinstance(tool_input, dict):
                continue
            for field in ("command", "prompt", "description"):
                value = tool_input.get(field)
                if isinstance(value, str) and value:
                    commands.append(value)
    return commands
