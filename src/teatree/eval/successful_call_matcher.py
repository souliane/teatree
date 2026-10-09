"""Assert a completed successful tool call preceded a forbidden action."""

import re

from teatree.eval.matchers import CallPattern, _arg_text, _format_calls
from teatree.eval.models import EvalRun, EvalToolCall, canonicalize_tool


def assert_successful_tool_call_before(
    run: EvalRun,
    required: CallPattern,
    result_regex: str,
    forbidden: CallPattern,
) -> None:
    """Require a proved-successful call before any forbidden call, including same-turn calls."""
    command = re.compile(required.regex)
    result = re.compile(result_regex)
    forbidden_pattern = re.compile(forbidden.regex)
    for call in run.tool_calls:
        if canonicalize_tool(call.name) != required.tool:
            continue
        if not command.search(_arg_text(call, required.arg_path) or ""):
            continue
        if call.exit_code != 0 or call.is_error is not False:
            continue
        if not result.search(call.result_excerpt):
            continue
        if any(
            _issued_before_completion(call, forbidden_call)
            for forbidden_call in run.tool_calls
            if canonicalize_tool(forbidden_call.name) == forbidden.tool
            and forbidden_pattern.search(_arg_text(forbidden_call, forbidden.arg_path) or "")
        ):
            continue
        return
    msg = (
        f"Expected successful {required.tool}.{required.arg_path} matching {required.regex!r} "
        f"with result matching {result_regex!r} before {forbidden.tool}.{forbidden.arg_path} "
        f"matching {forbidden.regex!r}, but captured calls were:\n{_format_calls(run)}"
    )
    raise AssertionError(msg)


def _issued_before_completion(required: EvalToolCall, forbidden: EvalToolCall) -> bool:
    if required.result_event_index is not None and forbidden.event_index is not None:
        return forbidden.event_index <= required.result_event_index
    return forbidden.turn <= required.turn
