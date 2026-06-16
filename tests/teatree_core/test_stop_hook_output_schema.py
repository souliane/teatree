"""Stop-hook output schema conformance (#1335).

The Claude Code hook JSON schema rejects ``hookSpecificOutput.additionalContext``
for ``Stop`` events — ``additionalContext`` is reserved for ``UserPromptSubmit``,
``PostToolUse`` and ``PostToolBatch``. A Stop handler that emits the
``hookSpecificOutput`` envelope with ``additionalContext`` triggers
"Hook JSON output validation failed — (root): Invalid input" and the nag
text is lost — the validator drops the whole turn-end payload.

The schema-valid soft-block channel for ``Stop`` is the top-level
``systemMessage`` string. It surfaces the body to the agent without
hard-blocking via ``decision: block`` (the soft-block intent both Stop
gates document in their comments).

This module asserts the POSITIVE shape — top-level ``systemMessage``
that contains the BLOCKING REMINDER / CONSIDERATION GATE body — and is
designed to flip RED if either Stop handler ever regresses back to the
``hookSpecificOutput.additionalContext`` envelope.
"""

import io
import json
from pathlib import Path
from typing import Any

import pytest

from hooks.scripts.hook_router import handle_consideration_gate, handle_enforce_answered_questions
from teatree.core.models import PendingChatInjection

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _run_handler(handler: Any, payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> str:
    """Invoke ``handler(payload)`` with captured stdout; return the raw stdout string."""
    buf = io.StringIO()
    monkeypatch.setattr("hooks.scripts.hook_router.sys.stdout", buf)
    handler(payload)
    return buf.getvalue()


def _write_transcript(tmp_path: Path, entries: list[dict[str, Any]]) -> str:
    path = tmp_path / "transcript.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")
    return str(path)


def _user_turn() -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": "do the thing"}}


def _assistant_edit(file_path: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "Edit",
                    "input": {"file_path": file_path, "old_string": "a", "new_string": "b"},
                }
            ],
        },
    }


class TestAnsweredQuestionsGateUsesSystemMessage:
    """``handle_enforce_answered_questions`` must emit top-level ``systemMessage``."""

    def test_payload_has_top_level_system_message_with_blocking_reminder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The positive-shape assertion: schema-valid Stop output.

        Anti-vacuous: revert the fix (emit ``hookSpecificOutput.additionalContext``
        for Stop) and this turns RED on three independent assertions —
        ``systemMessage`` is missing, ``hookSpecificOutput`` is present, and
        the body text moves into the wrong field.
        """
        PendingChatInjection.record(
            channel="D",
            slack_ts="1700000000.0001",
            text="why are some tests skipped?",
        )

        out = _run_handler(handle_enforce_answered_questions, {"session_id": "s1"}, monkeypatch)

        payload = json.loads(out)
        # POSITIVE: schema-valid top-level systemMessage carrying the nag.
        assert "systemMessage" in payload, (
            "Stop handler must emit top-level `systemMessage` — "
            "`hookSpecificOutput.additionalContext` is rejected by the Claude Code schema."
        )
        system_message = payload["systemMessage"]
        assert isinstance(system_message, str)
        assert system_message, "systemMessage must be non-empty"
        assert "BLOCKING REMINDER" in system_message
        assert "why are some tests skipped?" in system_message
        # NEGATIVE: must NOT carry the rejected envelope.
        assert "hookSpecificOutput" not in payload, (
            "Stop schema rejects `hookSpecificOutput.additionalContext`; the body belongs in top-level `systemMessage`."
        )
        # Soft-block intent: never hard-block via `decision: block`.
        assert "decision" not in payload


class TestConsiderationGateUsesSystemMessage:
    """``handle_consideration_gate`` must emit top-level ``systemMessage``."""

    def test_payload_has_top_level_system_message_with_consideration_gate_body(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Sibling Stop handler — same schema rule applies.

        Anti-vacuous: revert the fix and ``systemMessage`` disappears
        from the parsed JSON, ``hookSpecificOutput`` reappears, and the
        body migrates into the schema-rejected location.
        """
        transcript = _write_transcript(
            tmp_path,
            [
                _user_turn(),
                _assistant_edit("/Users/dev/.claude/settings.json"),
            ],
        )

        out = _run_handler(
            handle_consideration_gate,
            {"session_id": "s1", "transcript_path": transcript},
            monkeypatch,
        )

        payload = json.loads(out)
        assert "systemMessage" in payload, (
            "Stop handler must emit top-level `systemMessage` — "
            "`hookSpecificOutput.additionalContext` is rejected by the Claude Code schema."
        )
        system_message = payload["systemMessage"]
        assert isinstance(system_message, str)
        assert system_message, "systemMessage must be non-empty"
        assert "CONSIDERATION GATE" in system_message
        assert "settings.json" in system_message
        assert "hookSpecificOutput" not in payload
        assert "decision" not in payload
