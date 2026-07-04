"""Context compaction for Lane B — trim the conversation before each turn.

pydantic_ai's ``Agent`` in the pinned version has no ``history_processors``
constructor knob (it moved to the capability/hook surface), so the seam applies
compaction the provider-agnostic way: :func:`compact_history` runs over the
``message_history`` the session is about to feed the model, keeping the run
bounded on an unattended, many-turn dispatch. It preserves the FIRST message (the
task framing) and the most-recent ``keep_recent`` messages, dropping the stale
middle — the cheap, deterministic trim; a summarizing variant is a follow-up.

The cut never orphans a tool result: an OpenAI-compatible provider rejects a
``ModelRequest`` carrying a ``ToolReturnPart`` (or a tool-linked
``RetryPromptPart``) whose paired ``ToolCallPart`` was dropped with the trimmed
middle ("tool message without preceding tool_calls"). :func:`compact_history`
drops the orphaned leading tool-result messages so the kept window always opens
on a valid call→return pairing.
"""

from typing import TYPE_CHECKING

from pydantic_ai.messages import ModelRequest, ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic_ai.messages import ModelMessage

#: Default number of most-recent messages kept intact. Chosen so a normal
#: multi-turn coding trajectory never trims (it only engages on a runaway).
DEFAULT_KEEP_RECENT = 40


def compact_history(
    messages: "Sequence[ModelMessage]", *, keep_recent: int = DEFAULT_KEEP_RECENT
) -> "list[ModelMessage]":
    """Return a bounded copy of *messages*: the first + the last *keep_recent*.

    Under the trim threshold (``len <= keep_recent + 1``) the history is returned
    unchanged, so a normal conversation is byte-identical. Over it, the first
    message (task framing, the system context the model needs to stay on task) is
    preserved and the stale middle dropped, leaving the head plus the freshest
    *keep_recent* turns. Any orphaned tool-result messages at the head of the kept
    window — a ``ToolReturnPart`` whose ``ToolCallPart`` fell in the dropped middle
    — are trimmed too, so the window opens on a valid call→return pairing an
    OpenAI-compatible provider accepts. Deterministic and zero-token — no model call.
    """
    history = list(messages)
    if keep_recent < 1 or len(history) <= keep_recent + 1:
        return history
    tail = history[len(history) - keep_recent :]
    return [history[0], *tail[_leading_orphan_count(tail) :]]


def _leading_orphan_count(window: "list[ModelMessage]") -> int:
    """Count leading messages whose tool-results have no matching call kept in *window*.

    A cut can land so the first kept message is a ``ModelRequest`` carrying tool
    results whose paired ``ToolCallPart`` was in a dropped ``ModelResponse``. Those
    orphaned leading messages are dropped; scanning stops at the first message that
    is not an orphaned tool-result request, so a valid call→return pairing later in
    the window is never disturbed.
    """
    kept_call_ids = _tool_call_ids(window)
    dropped = 0
    for message in window:
        return_ids = _tool_result_ids(message)
        if not return_ids or return_ids <= kept_call_ids:
            break
        dropped += 1
    return dropped


def _tool_call_ids(window: "list[ModelMessage]") -> set[str]:
    """Every ``ToolCallPart.tool_call_id`` emitted by a ``ModelResponse`` in *window*."""
    return {
        part.tool_call_id
        for message in window
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart)
    }


def _tool_result_ids(message: "ModelMessage") -> set[str]:
    """The ``tool_call_id``s a ``ModelRequest`` returns, else the empty set.

    A ``ToolReturnPart`` and a tool-linked ``RetryPromptPart`` (``tool_name`` set —
    a gate/validation refusal of a specific call) both serialize as a tool message
    that needs a preceding ``tool_calls``; a plain ``RetryPromptPart`` (no
    ``tool_name``) is an output retry, not tool-linked, so it is excluded.
    """
    if not isinstance(message, ModelRequest):
        return set()
    ids: set[str] = set()
    for part in message.parts:
        if isinstance(part, ToolReturnPart) or (isinstance(part, RetryPromptPart) and part.tool_name is not None):
            ids.add(part.tool_call_id)
    return ids
