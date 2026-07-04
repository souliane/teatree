"""Context compaction for Lane B — trim the conversation before each turn.

pydantic_ai's ``Agent`` in the pinned version has no ``history_processors``
constructor knob (it moved to the capability/hook surface), so the seam applies
compaction the provider-agnostic way: :func:`compact_history` runs over the
``message_history`` the session is about to feed the model, keeping the run
bounded on an unattended, many-turn dispatch. It preserves the FIRST message (the
task framing) and the most-recent ``keep_recent`` messages, dropping the stale
middle — the cheap, deterministic trim; a summarizing variant is a follow-up.
"""

from typing import TYPE_CHECKING

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
    *keep_recent* turns. Deterministic and zero-token — no model call.
    """
    history = list(messages)
    if keep_recent < 1 or len(history) <= keep_recent + 1:
        return history
    return [history[0], *history[-keep_recent:]]
