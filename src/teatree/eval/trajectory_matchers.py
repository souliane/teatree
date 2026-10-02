"""Assertions whose evidence spans assistant text, hooks, and tool calls."""

import re

from teatree.eval.models import EvalRun, canonicalize_tool
from teatree.eval.text_normalization import normalize_match_text


def assert_visible_plan_before_first_tool(
    run: EvalRun,
    *,
    governed_tools: tuple[str, ...],
    patterns: tuple[str, ...],
) -> None:
    """Require the plan in the response, or in the first tool's durable allow audit."""
    governed = {canonicalize_tool(name) for name in governed_tools}
    calls = tuple(call for call in run.tool_calls if canonicalize_tool(call.name) in governed)
    if not calls:
        _assert_all_patterns("\n".join(run.text_blocks), patterns, subject="assistant response")
        return

    first_name = canonicalize_tool(calls[0].name)
    decisions = tuple(
        event
        for event in run.gate_events
        if event.hook_event_name == "PreToolUse"
        and event.sequence == 1
        and canonicalize_tool(event.tool_name) == first_name
    )
    if not decisions:
        msg = (
            f"First governed tool {first_name!r} has no sequence-1 durable PreToolUse decision; "
            "legacy or escaped tool calls fail closed."
        )
        raise AssertionError(msg)
    if any(event.outcome != "allow" or not event.tool_use_id for event in decisions):
        outcomes = ", ".join(f"{event.outcome or '<empty>'}:{event.tool_use_id or '<no-id>'}" for event in decisions)
        msg = f"First governed tool {first_name!r} was not durably allowed: {outcomes}"
        raise AssertionError(msg)
    snapshots = {event.assistant_text for event in decisions}
    if len(snapshots) != 1:
        msg = f"First governed tool {first_name!r} has inconsistent visible-text audit snapshots"
        raise AssertionError(msg)
    _assert_all_patterns(next(iter(snapshots)), patterns, subject="visible assistant text before first tool")


def _assert_all_patterns(text: str, patterns: tuple[str, ...], *, subject: str) -> None:
    if not text:
        msg = f"Expected a visible per-target plan in the {subject}, but it was empty."
        raise AssertionError(msg)
    normalized = normalize_match_text(text)
    for pattern in patterns:
        if re.search(pattern, normalized) is None:
            msg = f"Expected the {subject} to match regex {pattern!r}, but it was:\n  {text!r}"
            raise AssertionError(msg)
