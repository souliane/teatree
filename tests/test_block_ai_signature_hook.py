# test-path: cross-cutting — exercises hooks/scripts/ai_signature_guard.py and teatree.hooks.ai_signature_scan.
"""AI-signature PreToolUse behavior through the real parser and matcher."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hooks.scripts import t3_invocation
from hooks.scripts.ai_signature_guard import extract_ai_sig_payload, handle_block_ai_signature
from teatree.hooks import ai_signature_gate, ai_signature_scan


def _bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _pr(body: str) -> dict:
    return _bash(f"gh pr create --title t --body '{body}'")


def _reason(capsys: pytest.CaptureFixture[str]) -> str:
    return json.loads(capsys.readouterr().out)["permissionDecisionReason"]


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
def test_real_trailer_blocks_without_t3_or_subprocess(event: dict, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(t3_invocation, "t3_argv", lambda *_args: None)
    with patch.object(subprocess, "run", side_effect=AssertionError("scanner spawned a subprocess")):
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


def test_clean_body_allows_without_t3_or_subprocess(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(t3_invocation, "t3_argv", lambda *_args: None)
    with patch.object(subprocess, "run", side_effect=AssertionError("scanner spawned a subprocess")):
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
