"""Map the typed Agent-SDK messages onto the shared transcript extraction path.

The two fresh-run backends — the ``claude-agent-sdk`` runner
(:mod:`teatree.eval.api_runner`) and the ``pydantic_ai`` runner
(:mod:`teatree.eval.pydantic_ai_runner`) — both yield the SAME
``claude_agent_sdk`` message vocabulary (``AssistantMessage`` / ``ResultMessage`` /
``HookEventMessage``). That vocabulary is the provider-agnostic seam: this module
is the single adapter that renders each typed message to the stream-json event dict
the :mod:`teatree.eval.transcript` extractors already parse — the SAME path the
on-disk subscription transcript runner feeds — and folds the events into an
:class:`~teatree.eval.models.EvalRun`. So tool-call / text / terminal / cost
extraction is identical across every runner and the produced ``EvalRun`` is
byte-identical in shape regardless of which model produced the run.

The events are folded to :class:`~teatree.eval.transcript.StreamJsonEvent` DIRECTLY
via :meth:`~teatree.eval.transcript.StreamJsonEvent.from_obj` — the typed lane no
longer serializes each event dict to JSON only to re-parse it back into the same dict. The
synthesized ``raw_stdout`` (the stream-json text stored on the run for the report)
is still rendered, but the extractors read the event dicts, not a re-parse of that
string.

It is a deliberately separate concern from the runner's lifecycle (provisioning,
caps, terminal-cap handling): the runner OWNS *when* a trajectory is captured, this
module owns *how* a captured trajectory becomes a graded ``EvalRun``.
"""

import dataclasses
import json
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ContentBlock,
    Message,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import HookEventMessage

from teatree.eval.cost_observation import observe_cost
from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.transcript import (
    StreamJsonEvent,
    extract_billed_model,
    extract_gate_events,
    extract_model_cost_split,
    extract_terminal_reason,
    extract_text_blocks,
    extract_tool_calls,
    extract_usage,
    requested_model_present,
)


def eval_run_from_messages(spec: EvalSpec, messages: list[Message], *, price_from_usage: bool = True) -> EvalRun:
    """Map the typed SDK messages onto the shared transcript extraction path.

    Each typed message is rendered to a stream-json event dict and folded DIRECTLY
    into the :class:`~teatree.eval.transcript.StreamJsonEvent` list the extractors
    parse, so tool/text/terminal/cost extraction is identical to the on-disk
    transcript path with no serialize/deserialize round-trip.
    """
    event_dicts = [event for event in map(_message_to_event, messages) if event is not None]
    _enrich_pretool_audits(event_dicts)
    events = _events_from_dicts(event_dicts)
    raw_stdout = _render_stream_json(event_dicts)
    terminal_reason, is_error = extract_terminal_reason(events)
    present = requested_model_present(events, spec.model)
    split = extract_model_cost_split(events, spec.model)
    cost = observe_cost(events, requested_model=spec.model, price_from_usage=price_from_usage)
    return EvalRun(
        spec_name=spec.name,
        tool_calls=tuple(extract_tool_calls(events)),
        text_blocks=tuple(extract_text_blocks(events)),
        terminal_reason=terminal_reason,
        is_error=is_error,
        raw_stdout=raw_stdout,
        # The provider/run exception the non-CLI lanes report on an error-shaped
        # terminal message. Empty on a clean run, so a passing report is unchanged.
        raw_stderr=_terminal_error_text(events),
        cost_usd=cost.usd,
        cost_source=cost.source,
        usage=extract_usage(events),
        billed_model=extract_billed_model(events),
        fell_back=None if present is None else not present,
        main_cost_usd=split.main_cost_usd,
        aux_cost_usd=split.aux_cost_usd,
        main_usage=split.main_usage,
        aux_usage=split.aux_usage,
        gate_events=tuple(extract_gate_events(events)),
    )


def _terminal_error_text(events: list[StreamJsonEvent]) -> str:
    """The final ``result`` event's own error text, or ``""`` on a clean run — this lane's stderr.

    The non-CLI fresh-run lanes (``pydantic_ai`` / ``anthropic_api``) report a
    provider or run failure by yielding an error-shaped ``ResultMessage`` whose
    ``result`` field carries ``str(exc)`` and NOTHING else — the turn's tool blocks
    and text are never yielded on that path, so the trajectory is empty. Without
    this the whole cause is dropped: the report renders a bare
    ``error_during_execution`` with an empty transcript, which is indistinguishable
    from a model that simply answered nothing, and no lane artifact anywhere
    carries the exception.

    Only an ERRORED result is read — a successful run's ``result`` field is the
    model's final text, which is already extracted as a text block.
    """
    for event in reversed(events):
        if event.type != "result":
            continue
        subtype = event.subtype or "unknown"
        is_error_field = event.raw.get("is_error")
        is_error = bool(is_error_field) if is_error_field is not None else not subtype.startswith("success")
        if not is_error:
            return ""
        text = event.raw.get("result")
        return text if isinstance(text, str) else ""
    return ""


def _events_from_dicts(event_dicts: list[dict[str, Any]]) -> list[StreamJsonEvent]:
    events: list[StreamJsonEvent] = []
    for line_no, obj in enumerate(event_dicts, start=1):
        event = StreamJsonEvent.from_obj(line_no, obj)
        if event is not None:
            events.append(event)
    return events


def _render_stream_json(event_dicts: list[dict[str, Any]]) -> str:
    if not event_dicts:
        return ""
    return "\n".join(json.dumps(event) for event in event_dicts) + "\n"


@dataclasses.dataclass(frozen=True)
class _PreToolAudit:
    sequence: int
    tool_name: str
    tool_use_id: str
    assistant_text: str


def _enrich_pretool_audits(event_dicts: list[dict[str, Any]]) -> None:
    sequence = 0
    pending: list[_PreToolAudit] = []
    for event in event_dicts:
        if event.get("type") == "assistant" and event.get("parent_tool_use_id") is None:
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                pending = []
                continue
            assistant_text = "\n".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
            pending = []
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                sequence += 1
                pending.append(
                    _PreToolAudit(
                        sequence=sequence,
                        tool_name=str(block.get("name") or ""),
                        tool_use_id=str(block.get("id") or ""),
                        assistant_text=assistant_text,
                    )
                )
            continue
        if event.get("type") != "system" or event.get("subtype") != "hook_response":
            continue
        if event.get("hook_event") != "PreToolUse" or not pending:
            continue
        audit = pending.pop(0)
        for key, value in dataclasses.asdict(audit).items():
            if event.get(key) in {None, ""}:
                event[key] = value


def _message_to_event(message: Message) -> dict[str, Any] | None:
    if isinstance(message, HookEventMessage):
        # ``hook_started`` is lifecycle noise (no outcome yet); only the
        # ``hook_response`` (a hook that finished) carries the block decision the
        # gate-event extractor reads. Render it to the ``system``/``hook_response``
        # event shape :func:`~teatree.eval.transcript.extract_gate_events` parses.
        if message.subtype != "hook_response":
            return None
        data = message.data or {}
        return {
            "type": "system",
            "subtype": "hook_response",
            "hook_event": message.hook_event_name,
            "outcome": data.get("outcome"),
            "output": data.get("output"),
            "exit_code": data.get("exit_code"),
            "sequence": data.get("sequence"),
            "tool_name": data.get("tool_name"),
            "tool_use_id": data.get("tool_use_id"),
            "gate_id": data.get("gate_id"),
            "reason": data.get("reason"),
            "assistant_text": data.get("assistant_text"),
        }
    if isinstance(message, AssistantMessage):
        # ``parent_tool_use_id`` distinguishes a TOP-LEVEL (main-agent) turn —
        # ``None`` per the SDK contract — from a sub-agent SIDECHAIN turn, which
        # carries the parent ``Agent``/``Task`` tool_use id. Threading it through
        # to the synthesized event lets :func:`extract_tool_calls` count only the
        # main agent's own calls; a sub-agent's worktree ``.py`` edits, emitted
        # inline into the same ``query`` stream, must NOT be attributed to the main
        # agent (the #2596 mis-attribution that failed delegates/full_speed RED).
        return {
            "type": "assistant",
            "message": {"content": [_block_to_dict(b) for b in message.content]},
            "parent_tool_use_id": message.parent_tool_use_id,
        }
    if isinstance(message, ResultMessage):
        return {
            "type": "result",
            "subtype": message.subtype,
            "is_error": message.is_error,
            # The CLI's own result event carries this; dropping it discarded the ONLY
            # record of a provider/run exception, whose error path yields no other message.
            "result": message.result,
            "total_cost_usd": message.total_cost_usd,
            "usage": message.usage,
            "model_usage": message.model_usage,
        }
    return None


def _block_to_dict(block: ContentBlock) -> dict[str, Any]:
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": dict(block.input)}
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ThinkingBlock):
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    if isinstance(block, ToolResultBlock):
        # The pydantic_ai lane surfaces every tool result / gate refusal as a
        # ToolResultBlock (harness ``_tool_blocks_since``); rendering it to its
        # canonical block shape keeps the synthesized transcript faithful instead
        # of collapsing it to an opaque ``unknown``.
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
            "is_error": block.is_error,
        }
    return {"type": "unknown"}


__all__ = ["eval_run_from_messages"]
