"""Assertion helpers for :class:`EvalRun` results.

Each matcher raises ``AssertionError`` with the captured tool calls in the
message so a failed eval shows what the agent actually did, not just that
it didn't match.
"""

import re

from teatree.eval.models import EvalRun, EvalToolCall


def _get_arg(call: EvalToolCall, arg_path: str) -> object:
    value: object = call.input
    for part in arg_path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _format_calls(run: EvalRun) -> str:
    if not run.tool_calls:
        return "  (no tool calls captured)"
    return "\n".join(f"  - {c.name}({c.input!r})" for c in run.tool_calls)


def assert_tool_call_contains(run: EvalRun, tool_name: str, arg_path: str, substring: str) -> None:
    for call in run.tool_calls:
        if call.name != tool_name:
            continue
        value = _get_arg(call, arg_path)
        if isinstance(value, str) and substring in value:
            return
    msg = (
        f"Expected a {tool_name} tool call with {arg_path} containing {substring!r}, "
        f"but captured tool calls were:\n{_format_calls(run)}"
    )
    raise AssertionError(msg)


def assert_no_tool_call_matching(run: EvalRun, tool_name: str, arg_path: str, regex: str) -> None:
    pattern = re.compile(regex)
    for call in run.tool_calls:
        if call.name != tool_name:
            continue
        value = _get_arg(call, arg_path)
        if isinstance(value, str) and pattern.search(value):
            msg = (
                f"Did not expect any {tool_name} tool call with {arg_path} matching {regex!r}, "
                f"but found:\n  - {call.name}({call.input!r})\nAll captured tool calls:\n{_format_calls(run)}"
            )
            raise AssertionError(msg)
