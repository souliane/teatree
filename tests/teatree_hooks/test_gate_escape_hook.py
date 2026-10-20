import json
import subprocess
from unittest.mock import patch

from hooks.scripts import gate_escape as hook_escape
from hooks.scripts import hook_router


def _data(command: str) -> dict:
    return {
        "session_id": "session-1",
        "tool_use_id": "call-1",
        "tool_name": "Bash",
        "cwd": "/work/repo",
        "tool_input": {"command": command},
    }


def test_no_token_does_not_attempt_audit() -> None:
    with patch.object(hook_escape, "run_t3") as run:
        assert hook_escape.escape_requested(_data("git status"), "main_clone") is False
        assert hook_escape.escape_allows(_data("git status"), "main_clone") is False
    run.assert_not_called()


def test_matching_token_audits_the_exact_call_before_allow() -> None:
    data = _data("git status # [gate-escape:main_clone: fixture]")
    with (
        patch.object(
            hook_escape, "run_t3", return_value=subprocess.CompletedProcess([], 0, '{"allowed": true}', "")
        ) as run,
        patch.object(hook_escape, "t3_argv", return_value=["/usr/local/bin/t3", "tool", "gate-escape"]),
        patch.object(hook_escape, "_head", return_value="a" * 40),
    ):
        assert hook_escape.escape_requested(data, "main_clone") is True
        assert hook_escape.escape_allows(data, "main_clone") is True
    request = json.loads(run.call_args.kwargs["stdin_text"])
    assert request["gate"] == "main_clone"
    assert request["session_id"] == "session-1"
    assert request["call_id"] == "call-1"
    assert request["head"] == "a" * 40


def test_audit_failure_preserves_the_deny() -> None:
    data = _data("git status # [gate-escape:main_clone: fixture]")
    with (
        patch.object(hook_escape, "t3_argv", return_value=["/usr/local/bin/t3", "tool", "gate-escape"]),
        patch.object(hook_escape, "_head", return_value=""),
        patch.object(hook_escape, "run_t3", side_effect=OSError("unreachable")) as run,
    ):
        assert hook_escape.escape_allows(data, "main_clone") is False
    run.assert_called_once()


def test_shared_deny_writer_uses_escape_before_breaker() -> None:
    data = _data("git status # [gate-escape:main_clone: fixture]")
    with (
        patch.object(hook_router, "_current_hook_context", return_value=("PreToolUse", data)),
        patch.object(hook_escape, "escape_allows", return_value=True) as escape,
        patch.object(hook_router, "_apply_deny_circuit_breaker") as breaker,
    ):
        assert hook_router.emit_pretooluse_deny("blocked", gate_id="main_clone") is False
    escape.assert_called_once_with(data, "main_clone")
    breaker.assert_not_called()


def test_unrecorded_escape_cannot_be_auto_relaxed_by_breaker() -> None:
    data = _data("git status # [gate-escape:skill-loading-enforcement: fixture]")
    with (
        patch.object(hook_router, "_current_hook_context", return_value=("PreToolUse", data)),
        patch.object(hook_escape, "escape_allows", return_value=False),
        patch.object(hook_router, "_apply_deny_circuit_breaker") as breaker,
        patch.object(hook_router, "_write_pretooluse_deny", return_value=True) as deny,
    ):
        assert hook_router.emit_pretooluse_deny("blocked", gate_id="skill-loading-enforcement") is True
    breaker.assert_not_called()
    deny.assert_called_once()


def test_unreadable_parser_keeps_escape_request_in_deny_path() -> None:
    data = _data("git status # [gate-escape:main_clone: fixture]")
    with patch.object(hook_escape, "teatree_src_on_path", side_effect=OSError("unavailable")):
        assert hook_escape.escape_requested(data, "main_clone") is True


def test_missing_t3_refuses_and_names_the_cause(capsys) -> None:
    data = _data("git status # [gate-escape:main_clone: fixture]")
    with (
        patch.object(hook_escape, "t3_argv", return_value=None),
        patch.object(hook_escape, "run_t3") as run,
        patch.object(hook_escape, "_head", return_value=""),
    ):
        assert hook_escape.escape_allows(data, "main_clone") is False
    run.assert_not_called()
    assert "t3 is not on PATH" in capsys.readouterr().err
