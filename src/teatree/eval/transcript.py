"""Pure parser for ``claude -p --output-format stream-json`` output.

The CLI emits one JSON object per line. Event ``type`` values seen in the
wild: ``system`` (with ``subtype`` ``init``), ``assistant`` / ``user``
(turn messages containing content blocks), ``result`` (with ``subtype``
``success`` / ``error_max_turns`` / ``error_*``), and ``rate_limit_event``.

Tool-use extraction walks ``assistant.message.content[*]`` and keeps the
items whose ``type`` is ``tool_use`` — those carry ``name`` and ``input``
as the agent issued them. ``turn`` is 1-indexed over the order of
``assistant`` events in the stream.
"""

import dataclasses
import json
import re
from typing import Any

from teatree.eval.models import EvalToolCall, GateEvent, TokenUsage

#: Cap on the captured ``output`` snippet of a hook_response event — enough to
#: read the block reason, bounded so a verbose hook payload never bloats the run.
_GATE_OUTPUT_SNIPPET_CAP = 500

#: The four ``ResultMessage.usage`` keys the API bills on, mapped onto the
#: :class:`TokenUsage` fields. The mapping is the single place a future SDK
#: rename would have to be reflected; the conformance test pins these keys so a
#: silent drop fails loud rather than zeroing cost observability.
_USAGE_KEY_TO_FIELD: tuple[tuple[str, str], ...] = (
    ("input_tokens", "input"),
    ("cache_creation_input_tokens", "cache_creation"),
    ("cache_read_input_tokens", "cache_read"),
    ("output_tokens", "output"),
)

#: A per-model ``model_usage`` entry uses the CLI's camelCase keys (distinct from
#: the top-level ``usage`` snake_case above). The per-model ``costUSD`` is the key
#: fact that makes the main-vs-auxiliary cost split possible.
_MODEL_USAGE_KEY_TO_FIELD: tuple[tuple[str, str], ...] = (
    ("inputTokens", "input"),
    ("cacheCreationInputTokens", "cache_creation"),
    ("cacheReadInputTokens", "cache_read"),
    ("outputTokens", "output"),
)
_MODEL_COST_KEY = "costUSD"

#: A model id may carry a trailing ``-YYYYMMDD`` date suffix (``model_usage`` keys
#: are dated, the requested tag usually is not). The base id is the comparison key
#: for fallback detection and the cost split.
_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")

#: The documented short aliases (`--models opus,sonnet,haiku`, README/app.py) map
#: onto their full base ids. A requested tag may arrive as a short alias, so it is
#: normalized UP to the canonical full id at the same chokepoint a dated
#: ``model_usage`` key is normalized DOWN — otherwise ``opus`` never matches the
#: ``claude-opus-4-8`` usage key and fallback detection / the cost split break.
#:
#: GENERATION BUMP: these full ids are the current model generation and will
#: stale on the next generation. They are the same ids the sibling eval-layer
#: defaults carry — ``eval.loader.DEFAULT_MODEL`` / ``DEFAULT_JUDGE_MODEL``,
#: ``eval.api_runner.FALLBACK_MODEL``, and ``eval.models.EvalSpec.model`` /
#: ``JudgeSpec.model``. Bump all of those together; ``core.cost.PRICE_TABLE``
#: keys on the short tier name (``opus``), so it prices a dated bump correctly
#: without a new entry and is NOT part of this set.
_SHORT_ALIAS_TO_BASE: dict[str, str] = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
}


def _base_model_id(model: str) -> str:
    """Normalize a model id to its base form: short alias, ``@effort`` tag, and ``-YYYYMMDD`` date suffix.

    The requested tag is ``model[@effort]`` (effort is not a model) and may be a
    documented short alias (``opus``/``sonnet``/``haiku``); a ``model_usage`` key
    is the dated full model id. Both sides normalize through here — short aliases
    are mapped UP to the canonical full id, the date suffix is stripped — so a
    short-alias or dated request matches the full ``model_usage`` key.
    """
    base = _DATE_SUFFIX_RE.sub("", model.split("@", 1)[0])
    return _SHORT_ALIAS_TO_BASE.get(base, base)


@dataclasses.dataclass(frozen=True)
class StreamJsonEvent:
    line_no: int
    type: str
    subtype: str | None
    raw: dict[str, Any]

    @classmethod
    def from_obj(cls, line_no: int, obj: dict[str, Any]) -> "StreamJsonEvent | None":
        """Fold one already-parsed event dict into a :class:`StreamJsonEvent`.

        The shared alternative constructor both the on-disk transcript parser
        (:func:`parse_stream_json`) and the typed-message mapper
        (:mod:`teatree.eval.message_mapping`) fold through, so a synthesized fresh-run
        stream and a replayed transcript reach the extractors as the identical event
        shape — the typed lane skips the JSON string round-trip it used to pay
        (serialize each event only to re-parse it). Returns ``None`` for a dict with no
        string ``type``.
        """
        event_type = obj.get("type")
        if not isinstance(event_type, str):
            return None
        subtype_value = obj.get("subtype")
        subtype = subtype_value if isinstance(subtype_value, str) else None
        return cls(line_no=line_no, type=event_type, subtype=subtype, raw=obj)


def parse_stream_json(stdout: str) -> list[StreamJsonEvent]:
    events: list[StreamJsonEvent] = []
    for line_no, raw_line in enumerate(stdout.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        event = StreamJsonEvent.from_obj(line_no, obj)
        if event is not None:
            events.append(event)
    return events


def _is_subagent_event(event: StreamJsonEvent) -> bool:
    """True when *event* is a SUB-AGENT (tool-use sidechain) turn, not the main agent's.

    The SDK marks every TOP-LEVEL (main-agent) conversation message with
    ``parent_tool_use_id == None`` and every sub-agent SIDECHAIN message (the turns a
    dispatched ``Agent``/``Task`` produces, streamed inline into the SAME ``query``
    output) with the parent ``Agent``/``Task`` tool_use id. A non-``None``
    ``parent_tool_use_id`` is therefore the unambiguous sub-agent signal. The key is
    ABSENT on every replay/subscription fixture and on a real top-level turn, so an
    absent or ``None`` value is top-level (main agent) — the backward-compatible
    default that keeps the existing fixtures byte-identically graded.
    """
    return event.raw.get("parent_tool_use_id") is not None


def extract_tool_calls(events: list[StreamJsonEvent]) -> list[EvalToolCall]:
    """Tool calls the MAIN agent issued — sub-agent sidechain calls are excluded.

    A scenario grades the MAIN agent's behaviour; a tool call emitted by a
    dispatched sub-agent (its worktree ``.py`` edits, its ``pytest``/``git`` runs)
    is the sub-agent's, not the main agent's, and must not be attributed to it.
    ``_is_subagent_event`` filters those sidechain turns out via
    ``parent_tool_use_id`` so a correct delegate-then-stop main agent is not failed
    by a negative ``Edit/Write .py`` matcher firing on the SUB-agent's legitimate
    edits (#2596). ``turn`` stays 1-indexed over the MAIN-agent assistant events.
    """
    tool_calls: list[EvalToolCall] = []
    turn = 0
    for event in events:
        if event.type != "assistant":
            continue
        if _is_subagent_event(event):
            continue
        turn += 1
        message = event.raw.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "tool_use":
                continue
            name = item.get("name")
            tool_input = item.get("input")
            if not isinstance(name, str):
                continue
            tool_calls.append(
                EvalToolCall(
                    name=name,
                    input=dict(tool_input) if isinstance(tool_input, dict) else {},
                    turn=turn,
                ),
            )
    return tool_calls


def extract_gate_events(events: list[StreamJsonEvent]) -> list[GateEvent]:
    """Production-hook lifecycle events the runner synthesized into the stream.

    Only ``hook_response`` system events (a hook that COMPLETED) carry
    outcome/output; ``hook_started`` is dropped upstream by the message mapper.
    Returns one :class:`~teatree.eval.models.GateEvent` per response so the report
    can annotate a gate-assisted pass and the fail-loud / canary checks can confirm
    the shipped hooks fired under the eval wiring. A recorded-transcript replay
    carries no such events, so this returns ``[]`` there — the additive default.
    """
    gate_events: list[GateEvent] = []
    for event in events:
        if event.type != "system" or event.subtype != "hook_response":
            continue
        raw = event.raw
        name = raw.get("hook_event") or raw.get("hook_event_name") or ""
        gate_events.append(
            GateEvent(
                hook_event_name=str(name),
                outcome=_stringify(raw.get("outcome")),
                output_snippet=_stringify(raw.get("output"))[:_GATE_OUTPUT_SNIPPET_CAP],
            )
        )
    return gate_events


def _stringify(value: object) -> str:
    """A hook ``outcome``/``output`` may arrive as a str, dict, or None — render it flat."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def extract_text_blocks(events: list[StreamJsonEvent]) -> list[str]:
    text_blocks: list[str] = []
    for event in events:
        if event.type != "assistant":
            continue
        message = event.raw.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                continue
            text = item.get("text")
            if isinstance(text, str):
                text_blocks.append(text)
    return text_blocks


def extract_terminal_reason(events: list[StreamJsonEvent]) -> tuple[str, bool]:
    """Return ``(terminal_reason, is_error)`` from the final ``result`` event.

    When no ``result`` event is present (e.g. the CLI aborted before
    finishing), returns ``("aborted", True)`` per the spec.
    """
    for event in reversed(events):
        if event.type != "result":
            continue
        subtype = event.subtype or "unknown"
        is_error_field = event.raw.get("is_error")
        is_error = bool(is_error_field) if is_error_field is not None else not subtype.startswith("success")
        return subtype, is_error
    return "aborted", True


def extract_cost_usd(events: list[StreamJsonEvent]) -> float:
    """Return ``total_cost_usd`` from the final ``result`` event, or ``0.0``.

    The ``claude -p --output-format stream-json`` CLI embeds ``total_cost_usd``
    in the ``result`` event for metered (API-key) invocations. Subscription
    and offline runs omit the field, so this safely returns ``0.0`` there.
    """
    for event in reversed(events):
        if event.type != "result":
            continue
        raw_cost = event.raw.get("total_cost_usd")
        if isinstance(raw_cost, (int, float)):
            return float(raw_cost)
        return 0.0
    return 0.0


def extract_usage(events: list[StreamJsonEvent]) -> TokenUsage:
    """Return the ``usage`` token split from the final ``result`` event, all-zero when absent.

    Mirrors :func:`extract_cost_usd` defensively: a subscription / offline /
    capped run omits ``usage`` (and a metered run that drops a key, or carries a
    non-int value, must not crash cost observability) — every missing or
    non-int key defaults to ``0``, so the worst case is an all-zero
    :class:`TokenUsage`, never a raise.
    """
    for event in reversed(events):
        if event.type != "result":
            continue
        usage = event.raw.get("usage")
        if not isinstance(usage, dict):
            return TokenUsage()
        return TokenUsage(**_token_fields(usage))
    return TokenUsage()


def extract_billed_model(events: list[StreamJsonEvent]) -> str | None:
    """Return the model that actually ran — the dominant ``model_usage`` key — or ``None``.

    ``model_usage`` is a per-model usage map; the model that billed the most
    tokens is the one that ran (it differs from the requested model when
    ``fallback_model`` kicked in). Returns ``None`` when no ``result`` event, no
    ``model_usage``, or a malformed (non-dict / empty) map — the caller treats
    ``None`` as "not observable", never as a fallback signal.
    """
    for event in reversed(events):
        if event.type != "result":
            continue
        model_usage = event.raw.get("model_usage")
        if not isinstance(model_usage, dict) or not model_usage:
            return None
        # On a volume tie, max() keeps the first-seen key (Python's stable max) —
        # harmless, since a real fallback is lopsided, not a near-even split.
        return max(model_usage, key=lambda key: _model_usage_volume(model_usage[key]))
    return None


def requested_model_present(events: list[StreamJsonEvent], requested: str) -> bool | None:
    """Return whether the REQUESTED main model is present in ``model_usage`` — the fallback signal.

    Claude Code ALWAYS runs ``claude-haiku-4-5`` as a cheap auxiliary model
    alongside the requested main model, so an auxiliary key in ``model_usage`` is
    NORMAL — it is not a fallback. A fallback is the requested main model being
    SUBSTITUTED away: present here means absent from the ``model_usage`` keys.

    Comparison is on the base model id (effort tag stripped from *requested*, a
    ``-YYYYMMDD`` date suffix stripped from each ``model_usage`` key). Returns
    ``True`` when the requested model is present (NOT a fallback), ``False`` when
    it was substituted (a fallback), and ``None`` when ``model_usage`` is
    unobservable (no ``result`` event / absent / malformed map) — the caller
    treats ``None`` as "not observable", never as a fallback signal.
    """
    for event in reversed(events):
        if event.type != "result":
            continue
        model_usage = event.raw.get("model_usage")
        if not isinstance(model_usage, dict) or not model_usage:
            return None
        base_keys = {_base_model_id(key) for key in model_usage if isinstance(key, str)}
        return _base_model_id(requested) in base_keys
    return None


@dataclasses.dataclass(frozen=True)
class ModelCostSplit:
    """The metered cost + token usage of one run, split into MAIN vs AUXILIARY model.

    The MAIN model is the requested base model (the comparison number the
    benchmark cares about); AUXILIARY is the sum of every other ``model_usage``
    entry (Claude Code's background ``claude-haiku-4-5``). Both default to zero,
    so a non-metered / unobservable run yields an all-zero split, never a raise.
    """

    main_cost_usd: float = 0.0
    aux_cost_usd: float = 0.0
    main_usage: TokenUsage = dataclasses.field(default_factory=TokenUsage)
    aux_usage: TokenUsage = dataclasses.field(default_factory=TokenUsage)


def extract_model_cost_split(events: list[StreamJsonEvent], requested: str) -> ModelCostSplit:
    """Split the final ``result`` event's per-model cost/usage into MAIN vs AUXILIARY.

    Each ``model_usage`` entry carries a per-model ``costUSD`` and camelCase token
    counts. The requested base model's entry is the MAIN split; every other entry
    sums into the AUXILIARY split (the background ``claude-haiku-4-5``). A missing
    or malformed map yields an all-zero split — never a raise.
    """
    for event in reversed(events):
        if event.type != "result":
            continue
        model_usage = event.raw.get("model_usage")
        if not isinstance(model_usage, dict):
            return ModelCostSplit()
        return _split_model_usage(model_usage, requested)
    return ModelCostSplit()


def _split_model_usage(model_usage: dict[Any, Any], requested: str) -> ModelCostSplit:
    requested_base = _base_model_id(requested)
    main_cost = aux_cost = 0.0
    main_usage = aux_usage = TokenUsage()
    for key, entry in model_usage.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        cost = _model_cost(entry)
        usage = TokenUsage(**_model_token_fields(entry))
        if _base_model_id(key) == requested_base:
            main_cost += cost
            main_usage += usage
        else:
            aux_cost += cost
            aux_usage += usage
    return ModelCostSplit(main_cost_usd=main_cost, aux_cost_usd=aux_cost, main_usage=main_usage, aux_usage=aux_usage)


def _model_cost(entry: dict[Any, Any]) -> float:
    raw = entry.get(_MODEL_COST_KEY)
    return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0.0


def _model_token_fields(entry: dict[Any, Any]) -> dict[str, int]:
    """Map one ``model_usage`` entry's camelCase token keys onto :class:`TokenUsage` field ints."""
    return {field: _int_or_zero(entry.get(key)) for key, field in _MODEL_USAGE_KEY_TO_FIELD}


def _int_or_zero(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _token_fields(raw: dict[Any, Any]) -> dict[str, int]:
    """Map the four wire keys of a ``usage``/``model_usage`` entry onto field ints."""
    return {field: _int_or_zero(raw.get(key)) for key, field in _USAGE_KEY_TO_FIELD}


def _model_usage_volume(per_model: object) -> int:
    """Total token volume of one ``model_usage`` entry — the dominance key."""
    if not isinstance(per_model, dict):
        return 0
    return sum(_token_fields(per_model).values())
