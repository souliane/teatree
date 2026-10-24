# test-path: cross-cutting
"""The cache-wait PreToolUse handler reads live transcript usage before advising."""

import ast
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.cache_wait_advisory as advisory
from hooks.scripts.additional_context import emit_additional_context
from hooks.scripts.hook_router import _HANDLERS


def _event(transcript: Path, command: str = "gh run watch 123", **overrides: object) -> dict[str, object]:
    event = {
        "session_id": f"cache-wait-{uuid.uuid4().hex}",
        "tool_name": "Bash",
        "tool_input": {"command": command, "run_in_background": False},
        "transcript_path": str(transcript),
    }
    event.update(overrides)
    return event


def _transcript(path: Path, context_tokens: int) -> Path:
    usage = {
        "input_tokens": 10_000,
        "cache_read_input_tokens": context_tokens - 10_000,
        "cache_creation_input_tokens": 0,
    }
    path.write_text(json.dumps({"type": "assistant", "message": {"usage": usage}}) + "\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def interactive_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.delenv("CLAUDE_AGENT_SDK_VERSION", raising=False)


def test_large_interactive_foreground_wait_emits_one_parseable_advisory(tmp_path: Path) -> None:
    event = _event(_transcript(tmp_path / "transcript.jsonl", 50_000), "sleep 300")
    output = StringIO()
    with patch("sys.stdout", output):
        assert advisory.handle_warn_cache_wait(event) is False
    payload = json.loads(output.getvalue())
    assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "cache" in payload["hookSpecificOutput"]["additionalContext"].lower()


def test_advisory_yields_to_a_payload_already_written_on_the_call(tmp_path: Path) -> None:
    event = _event(_transcript(tmp_path / "transcript.jsonl", 50_000), "sleep 300")
    output = StringIO()
    with patch("sys.stdout", output):
        emit_additional_context("PreToolUse", "turn budget nudge")
        assert advisory.handle_warn_cache_wait(event) is False
    payload = json.loads(output.getvalue())
    assert payload["hookSpecificOutput"]["additionalContext"] == "turn budget nudge"


def test_router_emits_advisory_for_foreground_wait(tmp_path: Path) -> None:
    event = _event(_transcript(tmp_path / "transcript.jsonl", 50_000), "gh run watch 123")
    router = Path(__file__).resolve().parents[1] / "hooks" / "scripts" / "hook_router.py"
    environment = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    environment.pop("T3_CONFIG_DB", None)
    environment.pop("XDG_DATA_HOME", None)
    result = subprocess.run(
        [sys.executable, str(router), "--event", "PreToolUse"],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "cache" in payload["hookSpecificOutput"]["additionalContext"].lower()


@pytest.mark.parametrize(
    ("command", "overrides", "context_tokens"),
    [
        ("sleep 5", {}, 50_000),
        ("gh run watch 123", {"agent_id": "subagent"}, 50_000),
        ("gh run watch 123", {"tool_input": {"command": "gh run watch 123", "run_in_background": True}}, 50_000),
        ("gh run watch 123", {}, 49_999),
        ("gh run watch 123", {"tool_name": "Read"}, 50_000),
    ],
)
def test_ineligible_call_is_silent(
    tmp_path: Path, command: str, overrides: dict[str, object], context_tokens: int
) -> None:
    event = _event(_transcript(tmp_path / "transcript.jsonl", context_tokens), command, **overrides)
    output = StringIO()
    with patch("sys.stdout", output):
        assert advisory.handle_warn_cache_wait(event) is False
    assert output.getvalue() == ""


def test_unreadable_transcript_is_silent(tmp_path: Path) -> None:
    output = StringIO()
    with patch("sys.stdout", output):
        assert advisory.handle_warn_cache_wait(_event(tmp_path / "missing.jsonl")) is False
    assert output.getvalue() == ""


@pytest.mark.parametrize("lane", ["sdk", "unknown"])
def test_noninteractive_lane_is_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane: str) -> None:
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", lane)
    output = StringIO()
    with patch("sys.stdout", output):
        assert advisory.handle_warn_cache_wait(_event(_transcript(tmp_path / "transcript.jsonl", 50_000))) is False
    assert output.getvalue() == ""


def test_nonmatch_does_not_open_the_transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []

    def note_read(path: str) -> Iterator[dict]:
        opened.append(path)
        return iter(())

    monkeypatch.setattr(advisory, "iter_transcript_reversed", note_read)
    assert advisory.handle_warn_cache_wait(_event(tmp_path / "missing.jsonl", "git status")) is False
    assert opened == []


def test_handler_is_registered_and_has_no_deny_path() -> None:
    assert advisory.handle_warn_cache_wait in _HANDLERS["PreToolUse"]
    tree = ast.parse(Path(advisory.__file__).read_text(encoding="utf-8"))
    called = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert {"emit_pretooluse_deny", "_fail_open_or_deny"}.isdisjoint(called)
