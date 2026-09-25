import io
import json
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from hooks.scripts import codex_hook_adapter
from hooks.scripts.codex_hook_adapter import run_codex_hook


def test_pretool_deny_is_returned_in_codex_json_with_success_exit() -> None:
    deny = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "blocked",
        }
    }
    completed = CompletedProcess(["router"], 2, json.dumps(deny), "")

    with patch.object(codex_hook_adapter.subprocess, "run", return_value=completed):
        result = run_codex_hook("PreToolUse", "{}")

    assert result == (0, json.dumps(deny), "")


def test_non_json_router_failure_remains_a_failing_hook() -> None:
    completed = CompletedProcess(["router"], 2, "", "router crashed")

    with patch.object(codex_hook_adapter.subprocess, "run", return_value=completed):
        result = run_codex_hook("PreToolUse", "{}")

    assert result == (2, "", "router crashed")


@pytest.mark.parametrize(
    ("event", "returncode", "stdout"),
    [
        ("Stop", 2, '{"hookSpecificOutput":{"permissionDecision":"deny"}}'),
        ("PreToolUse", 0, '{"hookSpecificOutput":{"permissionDecision":"deny"}}'),
        ("PreToolUse", 2, "[]"),
        ("PreToolUse", 2, '{"hookSpecificOutput":[]}'),
        ("PreToolUse", 2, '{"hookSpecificOutput":{"permissionDecision":"ask"}}'),
    ],
)
def test_only_claude_pretool_denial_is_translated(event: str, returncode: int, stdout: str) -> None:
    completed = CompletedProcess(["router"], returncode, stdout, "detail")

    with patch.object(codex_hook_adapter.subprocess, "run", return_value=completed):
        result = run_codex_hook(event, "{}")

    assert result == (returncode, stdout, "detail")


def test_supported_event_payload_is_forwarded_unchanged() -> None:
    completed = CompletedProcess(["router"], 0, '{"systemMessage":"saved"}', "")

    with patch.object(codex_hook_adapter.subprocess, "run", return_value=completed) as run:
        result = run_codex_hook("SessionEnd", '{"session_id":"thread-1"}')

    assert result == (0, completed.stdout, "")
    assert run.call_args.kwargs["input"] == '{"session_id":"thread-1"}'


def test_router_spawn_failure_is_safe_and_visible() -> None:
    with patch.object(codex_hook_adapter.subprocess, "run", side_effect=OSError("missing")):
        result = run_codex_hook("Stop", "{}")

    assert result == (1, "", "TeaTree Codex hook adapter could not start the shared hook router.\n")


def test_main_writes_the_adapted_streams() -> None:
    with (
        patch.object(codex_hook_adapter.sys, "stdin", io.StringIO("{}")),
        patch.object(codex_hook_adapter.sys, "argv", ["codex_hook_adapter.py", "--event", "Stop"]),
        patch.object(codex_hook_adapter, "run_codex_hook", return_value=(0, '{"ok":true}', "note")),
        patch.object(codex_hook_adapter.sys, "stdout", new_callable=io.StringIO) as stdout,
        patch.object(codex_hook_adapter.sys, "stderr", new_callable=io.StringIO) as stderr,
    ):
        assert codex_hook_adapter.main() == 0

    assert stdout.getvalue() == '{"ok":true}'
    assert stderr.getvalue() == "note"
