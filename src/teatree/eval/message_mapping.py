"""Map the typed Agent-SDK messages onto the shared transcript extraction path.

The in-process SDK runner (:mod:`teatree.eval.api_runner`) yields the SDK's typed
:class:`~claude_agent_sdk.Message` objects. The grader, however, consumes the
stream-json event dicts the :mod:`teatree.eval.transcript` extractors parse — the
SAME path the subscription transcript runner feeds. This module is the single seam
that renders each typed message to that event dict and folds the parsed events into
an :class:`~teatree.eval.models.EvalRun`, so tool-call / text / terminal / cost
extraction is identical across both runners and the produced ``EvalRun`` is
byte-identical in shape.

It is a deliberately separate concern from the runner's lifecycle (provisioning,
caps, terminal-cap handling): the runner OWNS *when* a trajectory is captured, this
module owns *how* a captured trajectory becomes a graded ``EvalRun``.
"""

import json
from typing import Any

from claude_agent_sdk import AssistantMessage, ContentBlock, Message, ResultMessage, TextBlock, ToolUseBlock

from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.transcript import (
    extract_billed_model,
    extract_cost_usd,
    extract_model_cost_split,
    extract_terminal_reason,
    extract_text_blocks,
    extract_tool_calls,
    extract_usage,
    parse_stream_json,
    requested_model_present,
)


def eval_run_from_messages(spec: EvalSpec, messages: list[Message]) -> EvalRun:
    """Map the typed SDK messages onto the shared transcript extraction path.

    Each typed message is rendered to the stream-json event dict the
    :mod:`teatree.eval.transcript` extractors already parse, so tool/text/
    terminal/cost extraction is identical to the subscription transcript path.
    """
    raw_stdout = _synthesize_stream_json(messages)
    events = parse_stream_json(raw_stdout)
    terminal_reason, is_error = extract_terminal_reason(events)
    present = requested_model_present(events, spec.model)
    split = extract_model_cost_split(events, spec.model)
    return EvalRun(
        spec_name=spec.name,
        tool_calls=tuple(extract_tool_calls(events)),
        text_blocks=tuple(extract_text_blocks(events)),
        terminal_reason=terminal_reason,
        is_error=is_error,
        raw_stdout=raw_stdout,
        raw_stderr="",
        cost_usd=extract_cost_usd(events),
        usage=extract_usage(events),
        billed_model=extract_billed_model(events),
        fell_back=None if present is None else not present,
        main_cost_usd=split.main_cost_usd,
        aux_cost_usd=split.aux_cost_usd,
        main_usage=split.main_usage,
        aux_usage=split.aux_usage,
    )


def _synthesize_stream_json(messages: list[Message]) -> str:
    lines: list[str] = []
    for message in messages:
        event = _message_to_event(message)
        if event is not None:
            lines.append(json.dumps(event))
    return "\n".join(lines) + ("\n" if lines else "")


def _message_to_event(message: Message) -> dict[str, Any] | None:
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
            "total_cost_usd": message.total_cost_usd,
            "usage": message.usage,
            "model_usage": message.model_usage,
        }
    return None


def _block_to_dict(block: ContentBlock) -> dict[str, Any]:
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "name": block.name, "input": dict(block.input)}
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    return {"type": "unknown"}


__all__ = ["eval_run_from_messages"]
