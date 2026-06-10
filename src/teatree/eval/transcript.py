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
from typing import Any

from teatree.eval.models import EvalToolCall, TokenUsage

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


@dataclasses.dataclass(frozen=True)
class StreamJsonEvent:
    line_no: int
    type: str
    subtype: str | None
    raw: dict[str, Any]


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
        event_type = obj.get("type")
        if not isinstance(event_type, str):
            continue
        subtype_value = obj.get("subtype")
        subtype = subtype_value if isinstance(subtype_value, str) else None
        events.append(StreamJsonEvent(line_no=line_no, type=event_type, subtype=subtype, raw=obj))
    return events


def extract_tool_calls(events: list[StreamJsonEvent]) -> list[EvalToolCall]:
    tool_calls: list[EvalToolCall] = []
    turn = 0
    for event in events:
        if event.type != "assistant":
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
        return max(model_usage, key=lambda key: _model_usage_volume(model_usage[key]))
    return None


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
