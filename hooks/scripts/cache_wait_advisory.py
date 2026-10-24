"""Warn an interactive main agent before a long foreground wait with large context."""

import importlib

from hooks.scripts.additional_context import emit_additional_context, emitted_to_stdout
from hooks.scripts.managed_repo import teatree_src_on_path
from hooks.scripts.orchestration_boundary_signals import call_is_from_subagent
from hooks.scripts.question_gates import iter_transcript_reversed
from hooks.scripts.session_lane import LANE_INTERACTIVE_CLI, session_lane

LARGE_CONTEXT_TOKENS = 50_000
_TOKEN_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
_WAIT_PREFILTER = ("sleep", "gh", "glab")
_ADVISORY = (
    "[cache-wait] A foreground {wait} with {context_tokens:,} context tokens may expire prompt caching. "
    "Run the wait in background and return for the result, or compact before a known wait."
)


def _latest_context_tokens(transcript_path: str) -> int | None:
    for entry in iter_transcript_reversed(transcript_path):
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        values = [usage.get(key) for key in _TOKEN_KEYS]
        known = [value for value in values if isinstance(value, int) and not isinstance(value, bool) and value >= 0]
        if known:
            return sum(known)
    return None


def handle_warn_cache_wait(data: dict) -> bool:
    if emitted_to_stdout() or data.get("tool_name") != "Bash" or call_is_from_subagent(data):
        return False
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict) or tool_input.get("run_in_background") is True:
        return False
    command = tool_input.get("command")
    if not isinstance(command, str) or not any(token in command for token in _WAIT_PREFILTER):
        return False
    with teatree_src_on_path():
        detector = importlib.import_module("teatree.hooks.cache_wait_detect")
        wait = detector.declared_cache_expiring_wait(command)
    if wait is None or session_lane() != LANE_INTERACTIVE_CLI:
        return False
    context_tokens = _latest_context_tokens(str(data.get("transcript_path", "")))
    if context_tokens is not None and context_tokens >= LARGE_CONTEXT_TOKENS:
        emit_additional_context("PreToolUse", _ADVISORY.format(wait=wait, context_tokens=context_tokens))
    return False
