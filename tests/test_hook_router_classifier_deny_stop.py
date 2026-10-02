"""Tests for the classifier-denial STOP gate (#1247).

When the Claude Code auto-mode classifier denies a tool call, the agent must
STOP and explain (action / reason / minimum-unblock) instead of silently
retrying with a different argument shape, decomposing the command, or
switching tools — per the binding "Classifier Denial Protocol".

This is a two-stage hook:

1. **PostToolUse** (``handle_track_classifier_denial``): scans every tool
    result for the canonical denial string
    (``"denied by the Claude Code auto mode classifier"``). When detected,
    persists a per-session marker file containing the action fingerprint
    (tool_name plus a short input excerpt) so the Stop gate can name what
    was denied.
2. **Stop** (``handle_classifier_deny_stop_gate``): if the session marker
    exists, emits a top-level ``systemMessage`` instructing the agent to
    STOP and request explicit per-call authorization. Returns ``True`` to
    break the Stop chain (mirrors the consideration-gate pattern).

Recovery: the marker is cleared by the next ``UserPromptSubmit``
(``handle_clear_classifier_deny_marker``) — a fresh user turn re-arms the
gate. This matches the "Recovery path: the gate auto-disarms when the next
user message arrives" spec.

Fail-safe-to-empty: the PostToolUse handler returns silently when the
denial signal is absent or the data is malformed; the Stop gate returns
``None`` when no marker exists. The hook NEVER crashes the harness.

Integration-style: real handlers, real ``STATE_DIR`` on ``tmp_path``,
real JSON I/O exercised through stdin/stdout.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import (
    handle_classifier_deny_stop_gate,
    handle_clear_classifier_deny_marker,
    handle_track_classifier_denial,
)


@pytest.fixture
def gate_env(tmp_path: Path) -> Iterator[Path]:
    """Pin STATE_DIR to a fresh per-test directory.

    Yields the resolved STATE_DIR so tests can inspect the marker file
    written by the PostToolUse handler.
    """
    original_state = router.STATE_DIR
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        yield router.STATE_DIR
    finally:
        router.STATE_DIR = original_state


# ── Sample denial payloads (from real transcript JSONL) ──────────────


_CANONICAL_DENIAL = (
    "Permission for this action was denied by the Claude Code auto mode "
    "classifier. Reason: User asked an architectural question — not push "
    'approval; user\'s standing rule requires explicit "push" before any '
    "push.. If you have other tasks that don't depend on this action, "
    "continue working on those."
)


def _denial_posttooluse(
    *,
    tool_name: str = "Bash",
    tool_input: dict | None = None,
    session_id: str = "sess-1",
) -> dict:
    """Build a PostToolUse data dict that carries a classifier-denial response.

    The ``tool_response`` field mirrors the shape the harness emits on
    denial — a dict with ``is_error: true`` and an ``error``/``content``
    string containing the canonical denial preamble.
    """
    return {
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": tool_input or {"command": "gh pr merge 1234"},
        "tool_response": {
            "is_error": True,
            "error": _CANONICAL_DENIAL,
        },
    }


def _success_posttooluse(
    *,
    tool_name: str = "Bash",
    tool_input: dict | None = None,
    session_id: str = "sess-1",
) -> dict:
    """Build a PostToolUse data dict for a successful tool call (no denial)."""
    return {
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": tool_input or {"command": "ls"},
        "tool_response": {
            "is_error": False,
            "stdout": "file1\nfile2\n",
            "stderr": "",
        },
    }


def _stop_event(*, session_id: str = "sess-1", transcript_path: str = "") -> dict:
    return {
        "session_id": session_id,
        "transcript_path": transcript_path,
    }


def _user_prompt(*, session_id: str = "sess-1", prompt: str = "next thing") -> dict:
    return {
        "session_id": session_id,
        "prompt": prompt,
    }


# ── PostToolUse: detection of the denial signal ─────────────────────


class TestClassifierDenialDetection:
    """``handle_track_classifier_denial`` writes a marker iff the denial fires."""

    def test_denial_writes_marker(self, gate_env: Path) -> None:
        handle_track_classifier_denial(_denial_posttooluse())

        marker = gate_env / "sess-1.classifier-deny"
        assert marker.is_file(), "PostToolUse must persist a session marker on denial"
        payload = json.loads(marker.read_text(encoding="utf-8"))
        # Marker must carry enough context for the Stop gate to name the action.
        assert payload["tool_name"] == "Bash"
        assert "gh pr merge 1234" in payload["action"]

    def test_success_does_not_write_marker(self, gate_env: Path) -> None:
        handle_track_classifier_denial(_success_posttooluse())

        marker = gate_env / "sess-1.classifier-deny"
        assert not marker.exists(), "Successful tool calls must NOT trip the gate"

    def test_missing_tool_response_is_passthrough(self, gate_env: Path) -> None:
        # Some tools (notably AskUserQuestion historically) may not emit a
        # tool_response on every event. Fail-safe-to-empty: do nothing.
        handle_track_classifier_denial({"session_id": "sess-1", "tool_name": "Bash", "tool_input": {"command": "ls"}})
        assert not (gate_env / "sess-1.classifier-deny").exists()

    def test_unrelated_error_does_not_trip_gate(self, gate_env: Path) -> None:
        # An ordinary tool failure (e.g. `cat /nonexistent`) is an error but
        # NOT a classifier denial — only the canonical preamble should match.
        data = {
            "session_id": "sess-1",
            "tool_name": "Bash",
            "tool_input": {"command": "cat /nonexistent"},
            "tool_response": {
                "is_error": True,
                "error": "cat: /nonexistent: No such file or directory",
            },
        }
        handle_track_classifier_denial(data)
        assert not (gate_env / "sess-1.classifier-deny").exists()

    def test_denial_in_string_tool_response_is_detected(self, gate_env: Path) -> None:
        # Older harness versions may pass tool_response as a bare string.
        # The detector must still find the denial preamble.
        data = {
            "session_id": "sess-1",
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr merge 99"},
            "tool_response": "Error: " + _CANONICAL_DENIAL,
        }
        handle_track_classifier_denial(data)
        assert (gate_env / "sess-1.classifier-deny").is_file()

    def test_denial_in_content_field_is_detected(self, gate_env: Path) -> None:
        # The transcript shows the denial inside ``tool_result.content``; the
        # PostToolUse hook may also surface it in ``tool_response.content``.
        data = {
            "session_id": "sess-1",
            "tool_name": "Bash",
            "tool_input": {"command": "git push origin main"},
            "tool_response": {
                "is_error": True,
                "content": _CANONICAL_DENIAL,
            },
        }
        handle_track_classifier_denial(data)
        assert (gate_env / "sess-1.classifier-deny").is_file()

    def test_malformed_data_does_not_crash(self, gate_env: Path) -> None:
        # Fail-safe-to-empty: any malformed payload returns silently.
        for bad in ({}, {"tool_response": 42}, {"session_id": ""}, {"tool_response": None}):
            handle_track_classifier_denial(bad)
        # No marker written for any of these.
        assert not list(gate_env.glob("*.classifier-deny"))

    def test_missing_session_id_is_passthrough(self, gate_env: Path) -> None:
        # A denial without a session_id can't be persisted — fail-safe-to-empty.
        data = dict(_denial_posttooluse())
        data["session_id"] = ""
        handle_track_classifier_denial(data)
        assert not list(gate_env.glob("*.classifier-deny"))


# ── Stop: gate fires when marker exists ──────────────────────────────


class TestClassifierDenyStopGate:
    """``handle_classifier_deny_stop_gate`` emits a systemMessage on Stop."""

    def test_marker_present_emits_stop_systemmessage(self, gate_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # First the PostToolUse path records the denial.
        handle_track_classifier_denial(_denial_posttooluse())

        # Then the Stop hook fires.
        result = handle_classifier_deny_stop_gate(_stop_event())

        assert result is True, "Stop gate must return True to break the Stop chain"
        out = json.loads(capsys.readouterr().out)
        assert "systemMessage" in out, "Stop output must be a top-level systemMessage"
        body = out["systemMessage"]
        # Required wording from the spec.
        assert "Classifier denied" in body
        assert "STOP" in body
        assert "action" in body.lower()
        assert "reason" in body.lower()
        assert "minimum-unblock" in body
        # Must name what was denied so the agent can frame the request.
        assert "Bash" in body or "gh pr merge" in body

    def test_no_marker_is_passthrough(self, gate_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # No denial recorded => Stop gate stays silent.
        result = handle_classifier_deny_stop_gate(_stop_event())

        assert result is None, "Stop gate must return None when no denial is pending"
        assert capsys.readouterr().out == ""

    def test_missing_session_id_is_passthrough(self, gate_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # A Stop event without a session_id can't read the marker — fail-safe.
        result = handle_classifier_deny_stop_gate({"transcript_path": ""})
        assert result is None
        assert capsys.readouterr().out == ""

    def test_marker_for_other_session_does_not_fire(self, gate_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # Per-session isolation: a denial in session A must not gate session B.
        handle_track_classifier_denial(_denial_posttooluse(session_id="sess-A"))

        result = handle_classifier_deny_stop_gate(_stop_event(session_id="sess-B"))
        assert result is None
        assert capsys.readouterr().out == ""

    def test_marker_for_self_session_does_fire(self, gate_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # Sanity: the same session DOES trip the gate.
        handle_track_classifier_denial(_denial_posttooluse(session_id="sess-A"))

        result = handle_classifier_deny_stop_gate(_stop_event(session_id="sess-A"))
        assert result is True
        capsys.readouterr()  # drain


# ── UserPromptSubmit: recovery (auto-disarm) ─────────────────────────


class TestClassifierDenyRecovery:
    """A new user turn clears the marker so the gate auto-disarms."""

    def test_user_prompt_clears_marker(self, gate_env: Path) -> None:
        handle_track_classifier_denial(_denial_posttooluse())
        marker = gate_env / "sess-1.classifier-deny"
        assert marker.is_file()

        handle_clear_classifier_deny_marker(_user_prompt())

        assert not marker.exists(), "UserPromptSubmit must clear the marker"

    def test_user_prompt_without_marker_is_noop(self, gate_env: Path) -> None:
        # No marker to clear — handler must not crash.
        handle_clear_classifier_deny_marker(_user_prompt())
        assert not list(gate_env.glob("*.classifier-deny"))

    def test_user_prompt_clears_only_own_session(self, gate_env: Path) -> None:
        # Per-session: clearing session A leaves session B's marker intact.
        handle_track_classifier_denial(_denial_posttooluse(session_id="sess-A"))
        handle_track_classifier_denial(_denial_posttooluse(session_id="sess-B"))

        handle_clear_classifier_deny_marker(_user_prompt(session_id="sess-A"))

        assert not (gate_env / "sess-A.classifier-deny").exists()
        assert (gate_env / "sess-B.classifier-deny").is_file()

    def test_full_cycle_deny_stop_clear_stop_again(self, gate_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # End-to-end: denial → Stop fires → user prompt clears → Stop silent.
        handle_track_classifier_denial(_denial_posttooluse())
        assert handle_classifier_deny_stop_gate(_stop_event()) is True
        capsys.readouterr()

        handle_clear_classifier_deny_marker(_user_prompt())

        assert handle_classifier_deny_stop_gate(_stop_event()) is None
        assert capsys.readouterr().out == ""


# ── Router wiring: hook registration in _HANDLERS and hooks.json ─────


class TestRouterWiring:
    """Each handler must be registered in the right phase of ``_HANDLERS``."""

    def test_track_handler_registered_in_posttooluse(self) -> None:
        assert handle_track_classifier_denial in router._HANDLERS["PostToolUse"]

    def test_stop_gate_registered_in_stop(self) -> None:
        assert handle_classifier_deny_stop_gate in router._HANDLERS["Stop"]

    def test_clear_handler_registered_in_userpromptsubmit(self) -> None:
        assert handle_clear_classifier_deny_marker in router._HANDLERS["UserPromptSubmit"]


class TestHooksJsonRegistration:
    """Verify ``hooks/hooks.json`` wires the events used by the gate.

    ``hooks/hooks.json`` must route PostToolUse + Stop + UserPromptSubmit
    through ``hook_router.py`` — otherwise the in-process registration
    above is unreachable from the running harness.
    """

    def test_hooks_json_routes_required_events(self) -> None:
        # Walk up from this test file to the repo root to find hooks.json.
        repo_root = Path(__file__).resolve().parents[1]
        hooks_json = repo_root / "hooks" / "hooks.json"
        assert hooks_json.is_file(), f"hooks.json not found at {hooks_json}"
        config = json.loads(hooks_json.read_text(encoding="utf-8"))
        hooks = config.get("hooks", {})

        for event in ("PostToolUse", "Stop", "UserPromptSubmit"):
            assert event in hooks, f"hooks.json must register {event!r}"
            commands = " ".join(
                hook.get("command", "") for matcher_group in hooks[event] for hook in matcher_group.get("hooks", [])
            )
            assert "hook_router.py" in commands, f"{event} must route through hook_router.py for the gate to fire"
            assert f"--event {event}" in commands, f"{event} hook command must pass --event {event}"
