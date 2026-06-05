"""Adapt an in-session sub-agent JSONL into an :class:`EvalRun`.

The subscription eval path needs a transcript produced WITHOUT spending metered
API tokens. The only way to spend subscription tokens is an in-session ``Agent``
sub-agent — and Claude Code already writes every sub-agent's trajectory to
``~/.claude/projects/<slug>/<session-id>/subagents/agent-<id>.jsonl``. That file
is the bridge: the ``/t3:running-evals`` skill dispatches a sub-agent per
scenario, then points the subscription backend at the sub-agent JSONL.

That on-disk schema is the session envelope (see
:mod:`teatree.eval.session_transcript`), NOT the ``claude -p`` stream-json
schema (see :mod:`teatree.eval.transcript`). The two share an identical
``message.content[]`` block shape (``tool_use`` / ``text``), so tool-call and
text extraction is reused verbatim. They diverge at the terminus: a sub-agent
JSONL carries NO ``result`` event — completion is the final ``assistant``
message's ``stop_reason``. On disk that field is frequently ``null`` (the
streaming reason is not persisted), so a missing/non-string ``stop_reason`` is
treated as a clean completion, not an abort. This module supplies that
session-aware terminal reason and assembles the :class:`EvalRun` the grader
consumes, so a sub-agent transcript grades identically to a ``claude -p`` one.

This module never invokes ``claude -p`` or the Agent SDK: it only parses an
on-disk file, so the subscription lane stays unmetered end to end.
"""

import json

from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.transcript import StreamJsonEvent, extract_text_blocks, extract_tool_calls

_DIRTY_STOP_REASONS = frozenset({"max_tokens", "refusal", "error", "aborted"})


def is_subagent_transcript(raw: str) -> bool:
    """True when *raw* is a session-schema sub-agent JSONL, not ``claude -p`` stream-json.

    A sub-agent line carries the session envelope's ``isSidechain`` /
    ``agentId`` keys and never a top-level ``result`` event; the stream-json
    schema has neither envelope key and always ends in a ``result`` event.
    Detection reads the first well-formed object only, so it stays O(1).
    """
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        return "isSidechain" in obj or "agentId" in obj
    return False


def _as_stream_events(raw: str) -> list[StreamJsonEvent]:
    events: list[StreamJsonEvent] = []
    for line_no, raw_line in enumerate(raw.splitlines(), start=1):
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
        events.append(StreamJsonEvent(line_no=line_no, type=event_type, subtype=None, raw=obj))
    return events


def _terminal_reason(events: list[StreamJsonEvent]) -> tuple[str, bool]:
    """Return ``(terminal_reason, is_error)`` from the final ``assistant`` message.

    A sub-agent JSONL has no ``result`` event; the run's outcome is the last
    assistant turn's ``stop_reason``. On disk that field is commonly ``null`` (the
    streamed reason is not persisted), which is a clean finish — only an
    explicitly dirty reason (``max_tokens`` / ``refusal`` / ``error`` /
    ``aborted``) marks an errored run. No assistant event at all is an abort.
    """
    for event in reversed(events):
        if event.type != "assistant":
            continue
        message = event.raw.get("message")
        stop_reason = message.get("stop_reason") if isinstance(message, dict) else None
        if not isinstance(stop_reason, str):
            return "completed", False
        return stop_reason, stop_reason in _DIRTY_STOP_REASONS
    return "aborted", True


def subagent_run(spec: EvalSpec, raw: str) -> EvalRun:
    events = _as_stream_events(raw)
    terminal_reason, is_error = _terminal_reason(events)
    return EvalRun(
        spec_name=spec.name,
        tool_calls=tuple(extract_tool_calls(events)),
        text_blocks=tuple(extract_text_blocks(events)),
        terminal_reason=terminal_reason,
        is_error=is_error,
        raw_stdout=raw,
        raw_stderr="",
    )
