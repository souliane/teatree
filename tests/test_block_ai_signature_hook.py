# test-path: cross-cutting — exercises hooks/scripts/ai_signature_guard.py and teatree.hooks.ai_signature_scan.
"""AI-signature PreToolUse behavior through the real parser and matcher."""

import builtins
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from hooks.scripts.ai_signature_guard import extract_ai_sig_payload, handle_block_ai_signature
from teatree.hooks import ai_signature_gate, ai_signature_scan


def _bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _pr(body: str) -> dict:
    return _bash(f"gh pr create --title t --body '{body}'")


def _reason(capsys: pytest.CaptureFixture[str]) -> str:
    return json.loads(capsys.readouterr().out)["permissionDecisionReason"]


def _deny_scanner_import(monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    original_import = builtins.__import__

    def missing_scanner(
        name: str,
        global_scope: dict[str, object] | None = None,
        local_scope: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "teatree.hooks" and "ai_signature_scan" in fromlist:
            raise ModuleNotFoundError(message)
        return original_import(name, global_scope, local_scope, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", missing_scanner)


@pytest.mark.parametrize(
    "event",
    [
        _pr("body\n\nGenerated with [Claude Code]"),
        _bash("git commit -m 'fix: x\n\nCo-Authored-By: Claude <noreply@example.invalid>'"),
        {
            "tool_name": "mcp__github__create_pull_request",
            "tool_input": {"title": "t", "body": "desc\n\n\U0001f916 Generated with [Claude Code]"},
        },
    ],
    ids=["pr", "commit", "mcp"],
)
def test_real_trailer_blocks_without_t3_or_subprocess(event: dict, capsys) -> None:
    with (
        patch.object(subprocess, "run", side_effect=AssertionError("scanner spawned a subprocess")),
        patch.object(subprocess, "Popen", side_effect=AssertionError("scanner spawned a subprocess")),
    ):
        assert handle_block_ai_signature(event) is True
    assert "banned trailer" in _reason(capsys)


@pytest.mark.parametrize("flag", ["-F ", "-F", "-F="])
def test_git_message_file_forms_block(flag: str, tmp_path: Path, capsys) -> None:
    message = tmp_path / "message.txt"
    message.write_text("fix: x\n\nCo-Authored-By: Claude <noreply@example.invalid>\n", encoding="utf-8")
    assert handle_block_ai_signature(_bash(f"git commit {flag}{message}")) is True
    assert "banned trailer" in _reason(capsys)


@pytest.mark.parametrize("command", ["gh pr create --body-file {file}", "glab mr create --description-file {file}"])
def test_forge_body_file_forms_block(command: str, tmp_path: Path, capsys) -> None:
    body = tmp_path / "body.md"
    body.write_text("body\n\nGenerated with [Claude Code]\n", encoding="utf-8")
    assert handle_block_ai_signature(_bash(command.format(file=body))) is True
    assert "banned trailer" in _reason(capsys)


def test_scanner_failure_refuses_with_cause(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def broken_scanner(_body: str) -> list[tuple[int, str, str]]:
        message = "broken matcher"
        raise ValueError(message)

    monkeypatch.setattr(ai_signature_scan, "scan_text", broken_scanner)
    assert handle_block_ai_signature(_pr("clean body")) is True
    reason = _reason(capsys)
    assert "scanner error" in reason.lower()
    assert "broken matcher" in reason
    assert "banned trailer in the PR body" not in reason


@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp__github__create_pull_request",
        "mcp__github__update_pull_request",
        "mcp__glab__glab_mr_create",
        "mcp__glab__glab_mr_update",
    ],
)
def test_mcp_scan_failure_refuses_with_cause(tool_name: str, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _deny_scanner_import(monkeypatch, "matcher unavailable")
    data = {"tool_name": tool_name, "tool_input": {"body": "clean body"}}
    assert handle_block_ai_signature(data) is True
    reason = _reason(capsys)
    assert "scanner error" in reason.lower()
    assert "matcher unavailable" in reason


def test_extracted_tag_payload_scan_failure_refuses(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def broken_scanner(_body: str) -> list[tuple[int, str, str]]:
        message = "tag matcher crashed"
        raise ValueError(message)

    command = "git tag -a v1 -m 'release notes\n\nGenerated with [Claude Code]'"
    assert extract_ai_sig_payload(_bash(command)) is not None
    monkeypatch.setattr(ai_signature_scan, "scan_text", broken_scanner)
    assert handle_block_ai_signature(_bash(command)) is True
    assert "tag matcher crashed" in _reason(capsys)


@pytest.mark.parametrize(
    ("command", "expected_decision"),
    [("gh pr create --title t --body clean", "deny"), ("git status", "allow")],
)
def test_unimportable_scanner_names_import_failure(
    command: str, expected_decision: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _deny_scanner_import(monkeypatch, "No module named 'teatree.hooks.ai_signature_scan'")
    assert handle_block_ai_signature(_bash(command)) is (expected_decision == "deny")
    captured = capsys.readouterr()
    explanation = json.loads(captured.out)["permissionDecisionReason"] if expected_decision == "deny" else captured.err
    assert "ModuleNotFoundError" in explanation
    assert "ai_signature_scan" in explanation
    if expected_decision == "deny":
        assert "scan could not run" in explanation
    else:
        assert "NOTE" in explanation


@pytest.mark.parametrize(
    ("body", "exit_code"),
    [("body\n\nGenerated with [Claude Code]", 2), ("a clean description", 0)],
    ids=["trailer", "clean"],
)
def test_t3_off_path_router_bootstraps_scan(body: str, exit_code: int, tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    script = (
        "import importlib.util, sys\n"
        "assert importlib.util.find_spec('teatree') is None\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        "from hooks.scripts import hook_router as router\n"
        "for name in list(sys.modules):\n"
        "    if name == 'teatree' or name.startswith('teatree.'):\n"
        "        del sys.modules[name]\n"
        "assert importlib.util.find_spec('teatree') is None\n"
        "handlers = router._HANDLERS['PreToolUse']\n"
        "assert router.handle_block_ai_signature in handlers\n"
        "handlers[:] = [router.handle_block_ai_signature]\n"
        "router.main()\n"
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PATH"] = str(tmp_path)
    data = _pr(body)
    data["session_id"] = "bootstrap-test"
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, "--event", "PreToolUse"],
        input=json.dumps(data),
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=20,
    )
    assert result.returncode == exit_code, result.stderr + result.stdout
    if exit_code == 2:
        denial = json.loads(result.stdout)
        assert denial["gate_id"] == "ai_signature"
        assert "AI-signature" in denial["permissionDecisionReason"]
    else:
        assert result.stdout == ""


def _parser_crash(_command: str, _cwd: Path | None = None) -> str | None:
    message = "parser crash"
    raise ValueError(message)


@pytest.mark.parametrize(
    "command",
    ["ls -la", "git status"],
)
def test_internal_error_on_a_call_that_publishes_nothing_fails_open(
    command: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(ai_signature_gate, "extract_forge_post_body", _parser_crash)
    assert handle_block_ai_signature(_bash(command)) is False
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "parser crash" in captured.err


@pytest.mark.parametrize(
    "command",
    ["gh pr create --title t --body x", "glab mr note 1 -m x", "git commit -m 'fix: x'"],
    ids=["gh", "glab", "commit"],
)
def test_internal_error_on_a_publish_fails_closed_naming_the_cause(
    command: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(ai_signature_gate, "extract_forge_post_body", _parser_crash)
    assert handle_block_ai_signature(_bash(command)) is True
    reason = _reason(capsys)
    assert "scanner error" in reason.lower()
    assert "parser crash" in reason


def test_clean_body_allows_without_t3_or_subprocess(capsys) -> None:
    with (
        patch.object(subprocess, "run", side_effect=AssertionError("scanner spawned a subprocess")),
        patch.object(subprocess, "Popen", side_effect=AssertionError("scanner spawned a subprocess")),
    ):
        assert handle_block_ai_signature(_pr("a clean description\n\nRelates-to #836")) is False
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "command",
    ["git commit -C HEAD~1", "git commit -CHEAD~1", "git commit", "ls -la"],
)
def test_non_body_command_has_no_payload(command: str) -> None:
    assert extract_ai_sig_payload(_bash(command)) is None


@pytest.mark.parametrize("contents", [None, b"\x00\xff\xfe binary \x00"])
def test_missing_or_binary_message_file_has_no_payload(contents: bytes | None, tmp_path: Path) -> None:
    message = tmp_path / "message.txt"
    if contents is not None:
        message.write_bytes(contents)
    assert extract_ai_sig_payload(_bash(f"git commit -F {message}")) is None


@pytest.mark.parametrize("command", ["git commit -F{file}", "gh pr create --title t --body-file {file}"])
def test_clean_file_payload_allows(command: str, tmp_path: Path, capsys) -> None:
    body = tmp_path / "clean-body.txt"
    body.write_text("A clean description\n\nRelates-to #836\n", encoding="utf-8")
    assert handle_block_ai_signature(_bash(command.format(file=body))) is False
    assert capsys.readouterr().out == ""
