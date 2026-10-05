"""The hook gate ledger records bounded decisions without changing the deny."""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts import glab_stale_base_remote_guard, unbounded_wait_guard
from hooks.scripts.gate_ledger import write_gate_decision


def _rows(root: Path) -> list[dict]:
    return [json.loads(line) for path in (root / "otel").glob("gate-*.jsonl") for line in path.read_text().splitlines()]


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
    monkeypatch.setattr(router, "STATE_DIR", tmp_path / "hook-state")
    monkeypatch.setattr(router, "_CURRENT_EVENT", "PreToolUse")
    return tmp_path


@pytest.mark.parametrize(
    "case",
    [
        ("BLOCKED: private secret=top-secret", "plan_gate", ("plan_gate", "blocked")),
        ("sk_live_deadbeef: private", "sk_live_deadbeef", ("unknown", "unknown")),
    ],
)
def test_deny_records_only_allowlisted_markers(
    ledger: Path, capsys: pytest.CaptureFixture[str], case: tuple[str, str, tuple[str, str]]
) -> None:
    reason, gate_id, expected = case
    context = ("PreToolUse", {"session_id": "sensitive-session-id"})
    assert router._write_pretooluse_deny(reason, gate_id=gate_id, context=context)
    assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"
    row = _rows(ledger)[0]
    assert (row["gate"], row["rule"], row["decision"]) == (*expected, "deny")
    assert set(row) == {"epoch", "trace_id", "span_id", "gate", "decision", "rule", "session_ref"}
    assert re.fullmatch(r"[0-9a-f]{32}", row["trace_id"])
    assert re.fullmatch(r"[0-9a-f]{16}", row["span_id"])
    assert "private" not in str(row)
    assert "sensitive-session-id" not in str(row)


def test_two_blocked_guards_record_distinct_gate_markers(
    ledger: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(router, "_CURRENT_DATA", {"session_id": "two-guards"})
    monkeypatch.setattr(glab_stale_base_remote_guard, "_gate_enabled", lambda: True)
    monkeypatch.setattr(
        glab_stale_base_remote_guard,
        "stale_base_remote",
        lambda *_: "https://gitlab.com/another/project.git",
    )

    wait = {
        "session_id": "two-guards",
        "tool_name": "Bash",
        "tool_input": {"command": "until false; do sleep 5; done"},
    }
    stale_remote = {
        "session_id": "two-guards",
        "tool_name": "Bash",
        "cwd": str(ledger),
        "tool_input": {"command": "glab mr create -R group/project --title test"},
    }
    assert unbounded_wait_guard.handle_block_unbounded_wait(wait)
    assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"
    assert glab_stale_base_remote_guard.handle_block_glab_stale_base_remote(stale_remote)
    assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"
    assert [row["gate"] for row in _rows(ledger)] == ["unbounded_wait", "glab_stale_base_remote"]


@pytest.mark.parametrize("escape", ["_is_self_rescue", "_danger_gate_fail_open_enabled"])
def test_an_escaped_deny_records_an_override(
    ledger: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], escape: str
) -> None:
    monkeypatch.setattr(router, "_is_self_rescue", lambda _command: escape == "_is_self_rescue")
    monkeypatch.setattr(router, "_danger_gate_fail_open_enabled", lambda: escape == "_danger_gate_fail_open_enabled")
    data = {"session_id": "escaped", "tool_name": "Bash", "tool_input": {"command": "t3 teatree gate disable"}}

    assert router._fail_open_or_deny(data, "BLOCKED: plan first", gate_id="plan_gate") is False

    assert capsys.readouterr().out == ""
    assert [(row["gate"], row["decision"]) for row in _rows(ledger)] == [("plan_gate", "override")]


def test_ledger_failure_cannot_change_a_deny(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("hooks.scripts.gate_decision.write_gate_decision", side_effect=OSError("disk full")):
        assert router._write_pretooluse_deny("BLOCKED: private", gate_id="plan_gate", context=("PreToolUse", {}))
    assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"


def test_hook_only_append_prunes_gate_files_older_than_the_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "otel"
    directory.mkdir()
    today = datetime(2026, 9, 28, tzinfo=UTC)
    stale = directory / f"gate-{(today - timedelta(days=8)).date()}.jsonl"
    boundary = directory / f"gate-{(today - timedelta(days=7)).date()}.jsonl"
    stale.write_text("old\n")
    boundary.write_text("kept\n")
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
    with patch("hooks.scripts.gate_ledger.time.time", return_value=today.timestamp()):
        write_gate_decision(gate="plan_gate", decision="deny", rule="blocked", session_id="session")

    assert not stale.exists()
    assert boundary.read_text() == "kept\n"
    assert (directory / f"gate-{today.date()}.jsonl").exists()
