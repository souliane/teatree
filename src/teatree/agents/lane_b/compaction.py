"""Context compaction for Lane B — trim the conversation before each turn.

pydantic_ai's ``Agent`` in the pinned version has no ``history_processors``
constructor knob (it moved to the capability/hook surface), so the seam applies
compaction the provider-agnostic way: :func:`compact_history` runs over the
``message_history`` the session is about to feed the model, keeping the run
bounded on an unattended, many-turn dispatch. It preserves the FIRST message (the
task framing) and the most-recent ``keep_recent`` messages, dropping the stale
middle — the cheap, deterministic trim; a summarizing variant is a follow-up.

:func:`elide_stale_tool_results` is the second, per-model-request pass: the phased
Agent runs it through a ``ProcessHistory`` capability before EACH request, stubbing
stale tool results with a boundary that advances in steps so consecutive requests
share a byte-identical prefix.

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

from teatree.agents.lane_b.tool_names import TOOL_GREP, TOOL_READ
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

#: Trailing messages whose tool results stay verbatim, per model request, stepped; ``0`` disables.
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
            keep_tool_results=_resolve_phase_int(
                _COMPACTION_KEEP_TOOL_RESULTS_KEY, phase, DEFAULT_KEEP_TOOL_RESULTS, minimum=0
            ),
        )


def _resolve_phase_int(key: str, phase: str | None, default: int, *, minimum: int = 1) -> int:
    """*phase*'s entry in the DB override map at *key*, else *default*."""
    if not phase:
        return default
    raw = cold_reader.read_setting(key)
    if not isinstance(raw, dict):
        return default
    value = {str(name): val for name, val in raw.items()}.get(phase)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
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
    """
    resolved = policy if policy is not None else CompactionPolicy(keep_recent=keep_recent)
    keep = resolved.keep_recent
    history = list(messages)
    if keep < 1 or len(history) <= keep + 1:
        return history
    tail = history[len(history) - keep :]
    trimmed_tail = tail[_leading_orphan_count(tail) :]
    return [history[0], *trimmed_tail] if resolved.pin_head else trimmed_tail


def elide_stale_tool_results(
    messages: "Sequence[ModelMessage]", keep_tool_results: int = DEFAULT_KEEP_TOOL_RESULTS
) -> "list[ModelMessage]":
    """Stub each tool result before a boundary that advances in steps of *keep_tool_results*.

    Runs before every model request, so a short run the message trim never engages on
    stops re-sending every stale tool result. The call→return pairing, the tool names
    and the ``tool_call_id``s all survive, and the head is never touched.

    The boundary only moves once *keep_tool_results* more messages have arrived, leaving
    between K and 2K-1 messages verbatim: between steps each request extends the
    previous one byte-for-byte (earlier stubs are already in the run's state), so the
    provider's prefix cache keeps hitting. ``0`` disables the pass.
    """
    history = list(messages)
    stubbable = len(history) - keep_tool_results - 1
    if keep_tool_results < 1 or stubbable < keep_tool_results:
        return history
    cutoff = 1 + (stubbable // keep_tool_results) * keep_tool_results
    return [message if index == 0 or index >= cutoff else _stubbed(message) for index, message in enumerate(history)]


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


_ELIDED_PREFIX = "[elided: "

_REREADABLE_TOOLS = frozenset({TOOL_READ, TOOL_GREP})


def _stubbed_part(part: ToolReturnPart) -> ToolReturnPart:
    """*part* with its content replaced by the stub, unless it is one already or the stub would not be smaller.

    pydantic_ai writes each processed history back into the run, so an existing stub must
    survive untouched — re-stubbing it would overwrite the original size with the stub's own.
    """
    if isinstance(part.content, str) and part.content.startswith(_ELIDED_PREFIX):
        return part
    rendered = part.model_response_str()
    # A shell command may have side effects (commit, push, migrate): never invite a blind re-run.
    recovery = (
        "re-read to obtain them"
        if part.tool_name in _REREADABLE_TOOLS
        else "output elided; re-run only if the command is read-only"
    )
    stub = f"{_ELIDED_PREFIX}{len(rendered)} chars returned by `{part.tool_name}` on an earlier turn; {recovery}.]"
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
