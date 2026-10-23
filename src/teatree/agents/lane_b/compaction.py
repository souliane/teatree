"""Late context compaction for Lane B.

The phased agent runs :class:`LateHistoryCompactor` through ``ProcessHistory``
before every model request. It stubs stale tool results first, then summarizes
older turns near the model's token window while keeping recent turns and tool pairs.
``compact_history`` remains available for callers of the older message-count API.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from math import ceil
from typing import TYPE_CHECKING

import pydantic_ai
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RunUsage

from teatree.agents.lane_b.tool_names import TOOL_GREP, TOOL_READ
from teatree.config import cold_reader
from teatree.core.autocompact_advisory import native_context_window

if TYPE_CHECKING:
    from collections.abc import Sequence


#: Default number of most-recent messages kept intact. Chosen so a normal
#: multi-turn coding trajectory never trims (it only engages on a runaway).
DEFAULT_KEEP_RECENT = 40

#: The DB ``ConfigSetting`` key for the per-phase ``keep_recent`` override map,
#: e.g. ``{"coding": 60, "reviewing": 20}``. Read Django-free via
#: :mod:`teatree.config.cold_reader`; an absent/garbled value leaves the default.
_COMPACTION_KEEP_RECENT_KEY = "agent_compaction_keep_recent"

#: Trailing messages whose tool results stay verbatim, per model request, stepped; ``0`` disables.
DEFAULT_KEEP_TOOL_RESULTS = 6

DEFAULT_RESERVE_TOKENS = 16_000
DEFAULT_RECENT_TOKENS = 20_000


def context_window_for_model(
    model_name: str, reported_window: int | None, overrides: Mapping[str, int] | None = None
) -> int | None:
    configured = (overrides or {}).get(model_name)
    if isinstance(configured, int) and not isinstance(configured, bool) and configured > 0:
        return configured
    if reported_window is not None:
        return reported_window
    name = model_name.removeprefix("anthropic/")
    return native_context_window(name) or (1_000_000 if name.endswith("[1m]") else None)


class LateHistoryCompactor:
    def __init__(
        self,
        summarize: Callable[[str, RunUsage], Awaitable[str]],
        *,
        reserve_tokens: int = DEFAULT_RESERVE_TOKENS,
        keep_recent_tokens: int = DEFAULT_RECENT_TOKENS,
        keep_tool_results: int = DEFAULT_KEEP_TOOL_RESULTS,
        context_windows: Mapping[str, int] | None = None,
    ) -> None:
        self.summarize = summarize
        self.reserve_tokens = reserve_tokens
        self.keep_recent_tokens = keep_recent_tokens
        self.keep_tool_results = keep_tool_results
        self.context_windows = context_windows or {}
        self._force = False

    def force_next(self) -> None:
        self._force = True

    async def __call__(
        self, ctx: pydantic_ai.RunContext[object], messages: "list[ModelMessage]"
    ) -> "list[ModelMessage]":
        elided = elide_stale_tool_results(_drop_superseded_reads(messages), self.keep_tool_results)
        window = context_window_for_model(
            getattr(ctx.model, "model_name", ""), ctx.model.context_window, self.context_windows
        )
        forced, self._force = self._force, False
        if window is None and not forced:
            return elided
        unknown_window = window is None
        window = window or max(2 * self.reserve_tokens, 2 * _estimated_tokens(elided))
        current = _current_tokens(ctx, messages, elided)
        if not forced and current <= window - self.reserve_tokens:
            return elided
        start = len(elided)
        recent_tokens = min(self.keep_recent_tokens, max(1, current // 4) if unknown_window else window // 5)
        while start > 1 and _estimated_tokens(elided[start - 1 :]) < recent_tokens:
            start -= 1
        while start > 1 and _tool_result_ids(elided[start]) - _tool_call_ids(elided[start:]):
            start -= 1
        if start <= 1:
            return elided
        target = max(1, (window - self.reserve_tokens) // 2)
        while True:
            stale = ModelMessagesTypeAdapter.dump_json(elided[1:start]).decode()
            summary = await self.summarize(stale, ctx.usage)
            if not summary.strip():
                msg = "history summary was empty"
                raise ValueError(msg)
            summarized = ModelRequest(parts=[UserPromptPart(content=f"Earlier conversation summary:\n{summary}")])
            pinned = _pinned_operator_messages(elided[1:start])
            result = [elided[0], summarized, *pinned, *elided[start:]]
            if _estimated_tokens(result) <= target or start >= len(elided) - 1:
                return result
            while start < len(elided) - 1 and _estimated_tokens(result) > target:
                start += 1
                result = [elided[0], summarized, *_pinned_operator_messages(elided[1:start]), *elided[start:]]


def _pinned_operator_messages(messages: "Sequence[ModelMessage]") -> "list[ModelRequest]":
    return [
        message
        for message in messages
        if isinstance(message, ModelRequest)
        and any(
            isinstance(part, UserPromptPart)
            and isinstance(part.content, str)
            and part.content.startswith("[TeaTree operator input id=")
            for part in message.parts
        )
    ]


def _current_tokens(
    ctx: pydantic_ai.RunContext[object], messages: "Sequence[ModelMessage]", elided: "Sequence[ModelMessage]"
) -> int:
    used = ctx.context_window_used
    observed = used * ctx.model.context_window if used is not None and ctx.model.context_window else None
    elided_tokens = _estimated_tokens(elided)
    return (
        ceil(max(elided_tokens, observed - (_estimated_tokens(messages) - elided_tokens)))
        if observed
        else elided_tokens
    )


def _drop_superseded_reads(messages: "Sequence[ModelMessage]") -> "list[ModelMessage]":
    latest_by_path: dict[str, str] = {}
    superseded: set[str] = set()
    for message in messages:
        for part in message.parts:
            if not isinstance(part, ToolCallPart) or part.tool_name != TOOL_READ or not isinstance(part.args, dict):
                continue
            path = part.args.get("file_path") or part.args.get("path")
            if not isinstance(path, str) or not path:
                continue
            if prior := latest_by_path.get(path):
                superseded.add(prior)
            latest_by_path[path] = part.tool_call_id
    if not superseded:
        return list(messages)
    result: list[ModelMessage] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            result.append(message)
            continue
        parts = [
            replace(part, content="[superseded file read]")
            if isinstance(part, ToolReturnPart) and part.tool_call_id in superseded
            else part
            for part in message.parts
        ]
        result.append(replace(message, parts=parts))
    return result


def _estimated_tokens(messages: "Sequence[ModelMessage]") -> int:
    total = 0
    for message in messages:
        for part in message.parts:
            content = part.content if isinstance(part, UserPromptPart | ToolReturnPart | RetryPromptPart) else part
            total += max(1, len(str(content)) // 4)
    return total


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
    ``cheap`` compactor over the dropped middle) runs through ``ProcessHistory`` instead.
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
