"""Tests for the PreToolUse pre-dispatch quote-scanner gate (#1401).

Companion to the #1213 publish-boundary gate. This one scans the
``Agent``/``Task`` dispatch prompt BEFORE a sub-agent is spawned, so a
verbatim user-voice/PII fragment pasted into a brief as "context" never
reaches the sub-agent's model context (where it would later be echoed
into a published MR/issue/note, defeating the publish gate).

Integration-style: the real handler, the real detector
(``quote_scanner.scan_text``, reused — no second matcher), and the real
JSONL ledger pinned to ``tmp_path`` via ``T3_DATA_DIR``.

Synthetic fixtures only — no customer names, no real user quotes. The
user-voice shapes are neutral inventions that trip the existing HIGH
patterns.
"""

import json
import os
import sqlite3
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from hooks.scripts.hook_router import (
    handle_dispatch_prompt_quote_scanner,
    handle_dispatch_prompt_quote_scanner_on_task_create,
)
from teatree.hooks.quote_scanner import dispatch_quote_ok_reason, extract_dispatch_payload


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin the ledger + blocklist root AND the config DB to ``tmp_path`` so tests never touch real state."""
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "config.sqlite3"))
    return tmp_path


def _agent(prompt: str, *, description: str = "implement feature", tool_name: str = "Agent") -> dict:
    return {
        "session_id": "sess-1",
        "tool_name": tool_name,
        "tool_input": {
            "description": description,
            "prompt": prompt,
            "subagent_type": "t3:coder",
        },
    }


def _ledger_lines(tmp_path: Path) -> list[dict[str, object]]:
    ledger = tmp_path / "quote-scanner.jsonl"
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]


# A HIGH user-voice shape — a heading that explicitly announces a verbatim user
# block. Neutral synthetic content; trips the unconditional
# ``heading-user-ask-verbatim`` pattern (a bare ``## User mandate`` shape now
# downgrades to MEDIUM without adjacent quote evidence, #3240) without quoting any
# real person.
_HIGH_VOICE_PROMPT = "## User ask (verbatim, 2026-05-20)\n\nImplement the export endpoint and wire it to the dashboard."


class TestExtractDispatchPayload:
    """Payload extraction joins the dispatch subject + brief; passes through others."""

    def test_agent_joins_description_and_prompt(self) -> None:
        payload = extract_dispatch_payload("Agent", {"description": "subj", "prompt": "body"})
        assert payload == "subj\nbody"

    def test_task_tool_is_a_dispatch_surface(self) -> None:
        payload = extract_dispatch_payload("Task", {"prompt": "body only"})
        assert payload == "body only"

    def test_empty_dispatch_scans_empty_string_not_none(self) -> None:
        # A dispatch with no populated body is clean by construction — it
        # returns "" (scanned, finds nothing) rather than None (skipped).
        assert extract_dispatch_payload("Agent", {}) == ""

    @pytest.mark.parametrize("tool_name", ["Bash", "Edit", "Write", "Read", "Grep", "Skill"])
    def test_non_dispatch_tools_return_none(self, tool_name: str) -> None:
        assert extract_dispatch_payload(tool_name, {"prompt": "anything"}) is None


class TestDispatchQuoteOkReason:
    """The in-prompt ``[quote-ok: <reason>]`` opt-out token."""

    def test_token_with_reason_returns_reason(self) -> None:
        assert dispatch_quote_ok_reason("[quote-ok: paraphrase-impossible]\n\nbody") == "paraphrase-impossible"

    def test_token_inline_first_line(self) -> None:
        assert dispatch_quote_ok_reason("[quote-ok: legal-exact-wording] do the thing") == "legal-exact-wording"

    def test_empty_reason_is_rejected(self) -> None:
        assert dispatch_quote_ok_reason("[quote-ok: ]\n\nbody") is None

    def test_no_token_returns_none(self) -> None:
        assert dispatch_quote_ok_reason("an ordinary prompt with no token") is None

    def test_token_buried_past_head_window_is_ignored(self) -> None:
        prompt = ("x" * 600) + "[quote-ok: too-late]"
        assert dispatch_quote_ok_reason(prompt) is None


class TestHandlerDeny:
    """A HIGH user-voice/PII match in a dispatch prompt is denied with an actionable reason."""

    def test_high_voice_in_prompt_is_denied(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_dispatch_prompt_quote_scanner(_agent(_HIGH_VOICE_PROMPT))
        assert blocked is True
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"
        reason = decision["permissionDecisionReason"]
        # The reason must name the gate, what matched, and the unblock path.
        assert "pre-dispatch quote-scanner" in reason
        assert "quote-ok" in reason
        assert "paraphrase" in reason.lower()
        ledger = _ledger_lines(tmp_path)
        assert ledger
        assert ledger[-1]["decision"] == "deny"

    def test_high_voice_in_description_field_is_denied(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The subject field is scanned too — a quote pasted there is caught.
        data = _agent("ordinary brief body", description="## User ask (verbatim, 2026-05-20)")
        blocked = handle_dispatch_prompt_quote_scanner(data)
        assert blocked is True
        capsys.readouterr()
        assert _ledger_lines(tmp_path)[-1]["decision"] == "deny"

    def test_task_tool_treated_same_as_agent(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_dispatch_prompt_quote_scanner(_agent(_HIGH_VOICE_PROMPT, tool_name="Task"))
        assert blocked is True
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"


class TestHandlerAllow:
    """The opt-out token and clean/MEDIUM prompts pass without false-deny."""

    def test_quote_ok_token_bypasses_high_match(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        prompt = f"[quote-ok: exact-wording-required-for-repro]\n\n{_HIGH_VOICE_PROMPT}"
        blocked = handle_dispatch_prompt_quote_scanner(_agent(prompt))
        assert blocked is False
        assert capsys.readouterr().out == ""
        ledger = _ledger_lines(tmp_path)
        assert ledger
        assert ledger[-1]["decision"] == "allow-override"
        assert ledger[-1]["override"] is True

    def test_ordinary_prompt_is_allowed_no_false_deny(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # The common case: a normal author-voice brief with no user-voice
        # or PII shape. The fleet dispatches constantly — this MUST NOT deny.
        prompt = (
            "Implement issue #1401: add a PreToolUse branch that scans Agent/Task "
            "dispatch prompts. Reuse scan_text. Add tests. Run the gates and push."
        )
        blocked = handle_dispatch_prompt_quote_scanner(_agent(prompt))
        assert blocked is False
        assert capsys.readouterr().out == ""
        ledger = _ledger_lines(tmp_path)
        assert ledger
        assert ledger[-1]["decision"] == "allow"

    def test_medium_attribution_does_not_deny_on_dispatch(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # MEDIUM attribution shapes pass silently on dispatch (conservative:
        # only HIGH denies). No deny JSON, no stderr noise.
        blocked = handle_dispatch_prompt_quote_scanner(_agent("Per user direction, ship the export Friday."))
        assert blocked is False
        captured = capsys.readouterr()
        assert captured.out == ""
        assert _ledger_lines(tmp_path)[-1]["decision"] == "allow"


class TestToolScope:
    """Only Agent/Task tools trigger the gate; everything else passes untouched."""

    @pytest.mark.parametrize("tool_name", ["Bash", "Edit", "Write", "Read", "Grep", "AskUserQuestion", "Skill"])
    def test_non_dispatch_tools_pass_through(self, tmp_path: Path, tool_name: str) -> None:
        # Even with a HIGH-shaped payload, a non-dispatch tool is a no-op
        # here (the publish gate, not this one, governs Bash/Slack).
        data = {"session_id": "s", "tool_name": tool_name, "tool_input": {"prompt": _HIGH_VOICE_PROMPT}}
        assert handle_dispatch_prompt_quote_scanner(data) is False
        # A no-op never reaches the scan path, so the ledger stays empty.
        assert _ledger_lines(tmp_path) == []

    def test_non_dict_tool_input_is_a_noop(self, tmp_path: Path) -> None:
        data = {"session_id": "s", "tool_name": "Agent", "tool_input": "not-a-dict"}
        assert handle_dispatch_prompt_quote_scanner(data) is False


def _task(description: str, *, subject: str = "do work", session_id: str = "sess-1") -> dict:
    """A ``TaskCreated`` event payload (no ``tool_input`` — the fan-out schema)."""
    return {"session_id": session_id, "task_subject": subject, "task_description": description}


def _run_task(description: str, *, subject: str = "do work", session_id: str = "sess-1") -> tuple[bool, dict | None]:
    """Invoke the TaskCreated gate, capturing its ``continue:false`` stop envelope."""
    data = _task(description, subject=subject, session_id=session_id)
    out = StringIO()
    with patch("sys.stdout", out):
        blocked = handle_dispatch_prompt_quote_scanner_on_task_create(data)
    raw = out.getvalue().strip()
    return blocked, (json.loads(raw) if raw else None)


def _seed_gate_flag(*, value: bool) -> None:
    """Seed the DB-home ``dispatch_quote_gate_on_task_create_enabled`` flag in the hermetic config DB."""
    db = Path(os.environ["T3_CONFIG_DB"])
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) "
            "VALUES ('', 'dispatch_quote_gate_on_task_create_enabled', ?)",
            (json.dumps(value),),
        )
        conn.commit()
    finally:
        conn.close()


def _enable_task_gate() -> None:
    _seed_gate_flag(value=True)


class TestOnTaskCreateGate:
    """The TaskCreated dispatch-quote arm (#171): scans the fan-out task subject/description.

    The PreToolUse dispatch-quote gate keys on ``Agent``/``Task`` but the
    harness Workflow/Task fan-out (where dispatch prompts are actually created)
    BYPASSES ``PreToolUse`` — only ``TaskCreated`` reaches it. This arm rides
    that event. It ships default-OFF (opt-in, a #1640-class fan-out gate whose
    live behavior is unvalidated) and emits the ``TaskCreated`` teammate-stop
    envelope (``continue: false``), NOT the PreToolUse deny.
    """

    def test_high_quote_denies_when_enabled(self, tmp_path: Path) -> None:
        _enable_task_gate()
        blocked, payload = _run_task(_HIGH_VOICE_PROMPT)
        assert blocked is True
        assert payload is not None
        # TaskCreated deny schema — teammate-stop, not PreToolUse hookSpecificOutput.
        assert payload["continue"] is False
        assert "stopReason" in payload
        assert "permissionDecision" not in payload
        assert "pre-dispatch quote-scanner" in payload["stopReason"]
        assert _ledger_lines(tmp_path)[-1]["decision"] == "deny"

    def test_high_quote_in_subject_denies(self, tmp_path: Path) -> None:
        _enable_task_gate()
        blocked, payload = _run_task("ordinary brief", subject="## User ask (verbatim, 2026-05-20)")
        assert blocked is True
        assert payload is not None
        assert payload["continue"] is False

    def test_clean_task_is_allowed(self, tmp_path: Path) -> None:
        _enable_task_gate()
        blocked, payload = _run_task("Implement the export endpoint per the spec.")
        assert blocked is False
        assert payload is None
        assert _ledger_lines(tmp_path)[-1]["decision"] == "allow"

    def test_quote_ok_token_clears_high_match(self, tmp_path: Path) -> None:
        _enable_task_gate()
        blocked, payload = _run_task(f"[quote-ok: exact-wording-required]\n\n{_HIGH_VOICE_PROMPT}")
        assert blocked is False
        assert payload is None
        ledger = _ledger_lines(tmp_path)
        assert ledger[-1]["decision"] == "allow-override"
        assert ledger[-1]["override"] is True

    def test_default_off_passes_through_even_on_high_quote(self, tmp_path: Path) -> None:
        # No config row (flag unset) → the gate is inert by default. A HIGH
        # quote must pass through untouched (the gate ships opt-in pending #1640).
        blocked, payload = _run_task(_HIGH_VOICE_PROMPT)
        assert blocked is False
        assert payload is None
        # Inert ⇒ never reaches the scan/ledger path.
        assert _ledger_lines(tmp_path) == []

    def test_explicit_false_disables(self, tmp_path: Path) -> None:
        _seed_gate_flag(value=False)
        blocked, payload = _run_task(_HIGH_VOICE_PROMPT)
        assert blocked is False
        assert payload is None

    def test_broken_config_fails_disabled(self, tmp_path: Path) -> None:
        # A corrupt/unreadable config DB fails CLOSED to disabled (mirrors the #167
        # plan-gate-on-task-create posture): an unvalidated gate must never wedge
        # the fan-out on a config the operator can't read.
        db = Path(os.environ["T3_CONFIG_DB"])
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"not a sqlite database at all")
        blocked, payload = _run_task(_HIGH_VOICE_PROMPT)
        assert blocked is False
        assert payload is None

    def test_missing_session_id_passes_through(self, tmp_path: Path) -> None:
        _enable_task_gate()
        blocked, payload = _run_task(_HIGH_VOICE_PROMPT, session_id="")
        assert blocked is False
        assert payload is None
        assert _ledger_lines(tmp_path) == []
