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

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pydantic_ai.messages import ModelRequest, ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart

from teatree.config import cold_reader

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic_ai.messages import ModelMessage

#: Default number of most-recent messages kept intact. Chosen so a normal
#: multi-turn coding trajectory never trims (it only engages on a runaway).
DEFAULT_KEEP_RECENT = 40

#: The DB ``ConfigSetting`` key for the per-phase ``keep_recent`` override map,
#: e.g. ``{"coding": 60, "reviewing": 20}``. Read Django-free via
#: :mod:`teatree.config.cold_reader`; an absent/garbled value leaves the default.
_COMPACTION_KEEP_RECENT_KEY = "agent_compaction_keep_recent"

#: How many trailing messages keep their tool results verbatim. Well under
#: :data:`DEFAULT_KEEP_RECENT`, because the whole-message trim never engages on a
#: run of 20 turns while the tool results in it are re-sent on every request.
DEFAULT_KEEP_TOOL_RESULTS = 6

#: The DB ``ConfigSetting`` key for the per-phase ``keep_tool_results`` override map.
_COMPACTION_KEEP_TOOL_RESULTS_KEY = "agent_compaction_keep_tool_results"


@dataclass(frozen=True, slots=True)
class CompactionPolicy:
    """The context-compaction knobs, replacing the hardcoded head+tail trim (#3157 E2c).

    *keep_recent* is the most-recent-message window kept intact; *pin_head* preserves the
    first message (the task framing / system context) across the trim. Both are
    per-phase-configurable (:meth:`for_phase`), so a token-heavy phase can keep a wider
    window and a mechanical phase a tighter one — one DB row, no code edit. The default
    is byte-identical to the pre-policy constant trim. A summarizing tier (a model-tiered
    ``cheap`` compactor over the dropped middle) is a documented follow-up; this policy is
    the deterministic, zero-token trim.
    """

    keep_recent: int = DEFAULT_KEEP_RECENT
    pin_head: bool = True
    keep_tool_results: int = DEFAULT_KEEP_TOOL_RESULTS

    @classmethod
    def for_phase(cls, phase: str | None) -> "CompactionPolicy":
        """The policy for *phase*: the two ``agent_compaction_*`` overrides, else the defaults.

        An absent phase, an absent override map, or a non-integer entry all fall back to
        the shipped default so the behaviour is unchanged until an operator sets a row.
        """
        return cls(
            keep_recent=_resolve_phase_int(_COMPACTION_KEEP_RECENT_KEY, phase, DEFAULT_KEEP_RECENT),
            keep_tool_results=_resolve_phase_int(_COMPACTION_KEEP_TOOL_RESULTS_KEY, phase, DEFAULT_KEEP_TOOL_RESULTS),
        )


def _resolve_phase_int(key: str, phase: str | None, default: int) -> int:
    """*phase*'s entry in the DB override map at *key*, else *default*."""
    if not phase:
        return default
    raw = cold_reader.read_setting(key)
    if not isinstance(raw, dict):
        return default
    value = {str(name): val for name, val in raw.items()}.get(phase)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value


def compact_history(
    messages: "Sequence[ModelMessage]",
    *,
    keep_recent: int = DEFAULT_KEEP_RECENT,
    policy: CompactionPolicy | None = None,
) -> "list[ModelMessage]":
    """Return a bounded copy of *messages*: the pinned head + the last *keep_recent*.

    *policy* (a :class:`CompactionPolicy`) supersedes the bare *keep_recent* argument when
    supplied, so a phase-scoped policy drives the trim. Under the threshold
    (``len <= keep_recent + 1``) the history is returned unchanged, so a normal conversation
    is byte-identical. Over it, the pinned first message (task framing, the system context
    the model needs to stay on task) is preserved and the stale middle dropped, leaving the
    head plus the freshest *keep_recent* turns. Any orphaned tool-result messages at the head
    of the kept window — a ``ToolReturnPart`` whose ``ToolCallPart`` fell in the dropped
    middle — are trimmed too, so the window opens on a valid call→return pairing an
    OpenAI-compatible provider accepts. Deterministic and zero-token — no model call.

    Independently of that threshold, every tool result older than the last
    *keep_tool_results* messages is stubbed in place (:func:`_elide_stale_tool_results`)
    — the trim alone never engages on a short run, whose tool results are nonetheless
    re-sent on every request.
    """
    resolved = policy if policy is not None else CompactionPolicy(keep_recent=keep_recent)
    keep = resolved.keep_recent
    history = list(messages)
    if keep < 1 or len(history) <= keep + 1:
        return _elide_stale_tool_results(history, resolved.keep_tool_results)
    tail = history[len(history) - keep :]
    trimmed_tail = tail[_leading_orphan_count(tail) :]
    window = [history[0], *trimmed_tail] if resolved.pin_head else trimmed_tail
    return _elide_stale_tool_results(window, resolved.keep_tool_results)


def _elide_stale_tool_results(window: "list[ModelMessage]", keep_tool_results: int) -> "list[ModelMessage]":
    """Replace each tool result older than the last *keep_tool_results* messages with a stub.

    The whole-message trim only engages past ``keep_recent + 1`` messages, so a run of
    twenty turns is never compacted at all while every tool result in it is re-sent on
    every request — the dominant per-request cost on a metered lane. This pass shrinks
    the STALE ones in place: the call→return pairing, the tool names and the
    ``tool_call_id``s all survive, so the provider still accepts the history and the
    model still sees what it ran and in what order.

    Never the head (the task framing) and never the most-recent *keep_tool_results*
    messages, so the turn in flight keeps its results verbatim. ``0`` disables the pass.
    """
    cutoff = len(window) - keep_tool_results
    if keep_tool_results < 1 or cutoff <= 1:
        return window
    return [message if index == 0 or index >= cutoff else _stubbed(message) for index, message in enumerate(window)]


def _stubbed(message: "ModelMessage") -> "ModelMessage":
    """*message* with each oversized ``ToolReturnPart`` content replaced by its stub."""
    if not isinstance(message, ModelRequest):
        return message
    parts = [_stubbed_part(part) if isinstance(part, ToolReturnPart) else part for part in message.parts]
    return (
        message
        if all(new is old for new, old in zip(parts, message.parts, strict=True))
        else replace(message, parts=parts)
    )


def _stubbed_part(part: ToolReturnPart) -> ToolReturnPart:
    """*part* with its content replaced by the stub, unless the stub would not be smaller."""
    rendered = part.model_response_str()
    stub = (
        f"[elided: {len(rendered)} chars returned by `{part.tool_name}` on an earlier turn. Re-run it to obtain them.]"
    )
    return part if len(stub) >= len(rendered) else replace(part, content=stub)


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
