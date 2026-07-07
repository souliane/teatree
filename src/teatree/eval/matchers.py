"""Assertion helpers for :class:`EvalRun` results.

Each matcher raises ``AssertionError`` with the captured tool calls in the
message so a failed eval shows what the agent actually did, not just that
it didn't match.
"""

import dataclasses
import json
import re

from teatree.eval.models import EvalRun, EvalToolCall, canonicalize_tool


@dataclasses.dataclass(frozen=True)
class CallPattern:
    """A ``(tool, arg_path, regex)`` triple naming one tool-call shape to look for.

    Groups the forbidden call and the order guard of an order-aware negative into
    one cohesive value, so :func:`assert_no_tool_call_before` takes two patterns
    instead of six loose strings.
    """

    tool: str
    arg_path: str
    regex: str


def _get_arg(call: EvalToolCall, arg_path: str) -> object:
    value: object = call.input
    for part in arg_path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _as_text(value: object) -> str | None:
    """Comparable string form of an arg value, or ``None`` if not matchable.

    A string compares as itself. A boolean / number (e.g. Bash's
    ``run_in_background: true``) compares as its ``str()`` form so a matcher
    can pin it. A list/dict argument (e.g. ``AskUserQuestion``'s structured
    ``questions`` list, ``TaskCreate``'s structured fields) is JSON-serialized so
    a regex matcher can search its contents — without this a structured-arg tool
    is unmatchable and the scenario silently vacuous. ``None`` is not matchable.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int | float):
        return str(value)
    if isinstance(value, list | dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return None


def _format_calls(run: EvalRun) -> str:
    if not run.tool_calls:
        return "  (no tool calls captured)"
    return "\n".join(f"  - {c.name}({c.input!r})" for c in run.tool_calls)


def assert_tool_call_contains(run: EvalRun, tool_name: str, arg_path: str, substring: str) -> None:
    for call in run.tool_calls:
        if canonicalize_tool(call.name) != tool_name:
            continue
        # An absent arg compares as "" so a tool-presence matcher (substring "")
        # passes when the agent calls the tool with no/omitted arg — e.g. a
        # correct ``TaskList()`` that reads the whole live list. A specific
        # substring still fails against "", so value-pinning matchers are unchanged.
        value = _as_text(_get_arg(call, arg_path)) or ""
        if substring in value:
            return
    msg = (
        f"Expected a {tool_name} tool call with {arg_path} containing {substring!r}, "
        f"but captured tool calls were:\n{_format_calls(run)}"
    )
    raise AssertionError(msg)


def assert_tool_call_matching(run: EvalRun, tool_name: str, arg_path: str, regex: str) -> None:
    pattern = re.compile(regex)
    for call in run.tool_calls:
        if canonicalize_tool(call.name) != tool_name:
            continue
        # An absent arg compares as "" so a tool-presence matcher (``~ ".*"``)
        # passes when the agent calls the tool with no/omitted arg — e.g. a
        # correct ``TaskList()`` reading the whole live list. A value-pinning
        # regex (``~ "in_progress"``) still fails against "", so it is unchanged.
        value = _as_text(_get_arg(call, arg_path)) or ""
        if pattern.search(value):
            return
    msg = (
        f"Expected a {tool_name} tool call with {arg_path} matching regex {regex!r}, "
        f"but captured tool calls were:\n{_format_calls(run)}"
    )
    raise AssertionError(msg)


def assert_no_tool_call_contains(run: EvalRun, tool_name: str, arg_path: str, substring: str) -> None:
    for call in run.tool_calls:
        if canonicalize_tool(call.name) != tool_name:
            continue
        value = _as_text(_get_arg(call, arg_path))
        if value is not None and substring in value:
            msg = (
                f"Did not expect any {tool_name} tool call with {arg_path} containing {substring!r}, "
                f"but found:\n  - {call.name}({call.input!r})\nAll captured tool calls:\n{_format_calls(run)}"
            )
            raise AssertionError(msg)


def assert_no_tool_call_matching(run: EvalRun, tool_name: str, arg_path: str, regex: str) -> None:
    pattern = re.compile(regex)
    for call in run.tool_calls:
        if canonicalize_tool(call.name) != tool_name:
            continue
        value = _as_text(_get_arg(call, arg_path))
        if value is not None and pattern.search(value):
            msg = (
                f"Did not expect any {tool_name} tool call with {arg_path} matching {regex!r}, "
                f"but found:\n  - {call.name}({call.input!r})\nAll captured tool calls:\n{_format_calls(run)}"
            )
            raise AssertionError(msg)


def _first_matching_turn(run: EvalRun, target: CallPattern) -> int | None:
    """The ``turn`` of the FIRST tool call matching *target*, or ``None``."""
    pattern = re.compile(target.regex)
    for call in run.tool_calls:
        if canonicalize_tool(call.name) != target.tool:
            continue
        value = _as_text(_get_arg(call, target.arg_path))
        if value is not None and pattern.search(value):
            return call.turn
    return None


def assert_no_tool_call_before(run: EvalRun, forbidden: CallPattern, guard: CallPattern) -> None:
    """Assert no *forbidden* call precedes the FIRST *guard* call (order-aware negative).

    The forbidden call reds the run ONLY when it occurs at a turn STRICTLY BEFORE
    the first guard call. When the guard call never fires, its turn is treated as
    ``+inf`` so EVERY forbidden call is "before" it — the correct strict reading
    (the guard action never happened, so nothing was allowed to precede it). A
    forbidden call at or after the guard is permitted, so the compliant "do the
    guard action FIRST, then the guarded one" trajectory passes.
    """
    guard_turn = _first_matching_turn(run, guard)
    pattern = re.compile(forbidden.regex)
    for call in run.tool_calls:
        if canonicalize_tool(call.name) != forbidden.tool:
            continue
        value = _as_text(_get_arg(call, forbidden.arg_path))
        if value is None or not pattern.search(value):
            continue
        if guard_turn is None or call.turn < guard_turn:
            guard_desc = f"{guard.tool}.{guard.arg_path} matching {guard.regex!r}"
            msg = (
                f"Did not expect any {forbidden.tool} tool call with {forbidden.arg_path} "
                f"matching {forbidden.regex!r} BEFORE the first {guard_desc}, but found:\n"
                f"  - {call.name}({call.input!r})\nAll captured tool calls:\n{_format_calls(run)}"
            )
            raise AssertionError(msg)


def _final_assistant_message(run: EvalRun) -> str | None:
    """The run's terminal assistant text — the END STATE of the scenario.

    The last ``text_blocks`` entry is the agent's final message (after every tool
    call resolved). ``None`` when the run emitted no assistant text at all, so the
    final-state matchers can report "no final assistant message" rather than
    matching a phantom empty string.
    """
    return run.text_blocks[-1] if run.text_blocks else None


def assert_final_state_matching(run: EvalRun, regex: str) -> None:
    """Assert the run's FINAL assistant message matches *regex* (end-state check)."""
    final = _final_assistant_message(run)
    if final is None:
        msg = f"Expected a final assistant message matching regex {regex!r}, but there was no final assistant message."
        raise AssertionError(msg)
    if not re.search(regex, final):
        msg = f"Expected the final assistant message to match regex {regex!r}, but it was:\n  {final!r}"
        raise AssertionError(msg)


def assert_final_state_contains(run: EvalRun, substring: str) -> None:
    """Assert the run's FINAL assistant message contains *substring* (end-state check)."""
    final = _final_assistant_message(run)
    if final is None:
        msg = f"Expected a final assistant message containing {substring!r}, but there was no final assistant message."
        raise AssertionError(msg)
    if substring not in final:
        msg = f"Expected the final assistant message to contain {substring!r}, but it was:\n  {final!r}"
        raise AssertionError(msg)
