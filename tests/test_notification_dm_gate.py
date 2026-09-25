"""The Notification-event DM gate: surface a stalled AskUserQuestion (#4818).

``AskUserQuestion`` is not itself a ``Notification`` matcher (Claude Code's own
docs), so the handler gates on the transcript SHAPE instead — a trailing,
still-blocking ``AskUserQuestion`` tool_use with no ``tool_result`` after it —
rather than an unconfirmed Notification sub-type field. Integration-style: a
real transcript JSONL under ``tmp_path``, the real handler, only the ``t3``
shell-out mocked (no live Slack egress in a unit test).
"""

import json
from pathlib import Path
from unittest.mock import patch

import hooks.scripts.hook_router as router
import hooks.scripts.notification_dm_gate as gate
from hooks.scripts.notification_dm_gate import (
    _pending_ask_text,
    _stalled_ask_idempotency_key,
    handle_notify_stalled_ask,
)


def _ask_block(question: str) -> dict:
    tool_use = {"type": "tool_use", "name": "AskUserQuestion", "input": {"questions": [{"question": question}]}}
    return {"type": "assistant", "message": {"role": "assistant", "content": [tool_use]}}


def _answered(question: str) -> list[dict]:
    """A resolved ask: the tool_use is followed by its tool_result."""
    return [
        _ask_block(question),
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "tool_result", "content": "1"}]},
        },
    ]


def _write_transcript(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


class TestPendingAskText:
    def test_trailing_unanswered_ask_is_found(self, tmp_path: Path) -> None:
        transcript = _write_transcript(tmp_path, [_ask_block("Approve the deploy?")])
        assert _pending_ask_text(str(transcript)) == "Approve the deploy?"

    def test_an_already_answered_ask_is_not_pending(self, tmp_path: Path) -> None:
        """Control: the tool_result landing means the call already returned."""
        transcript = _write_transcript(tmp_path, _answered("Approve the deploy?"))
        assert _pending_ask_text(str(transcript)) is None

    def test_trailing_prose_with_no_ask_is_not_pending(self, tmp_path: Path) -> None:
        transcript = _write_transcript(
            tmp_path,
            [{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Done."}]}}],
        )
        assert _pending_ask_text(str(transcript)) is None

    def test_missing_transcript_is_not_pending(self, tmp_path: Path) -> None:
        assert _pending_ask_text(str(tmp_path / "missing.jsonl")) is None

    def test_empty_question_text_is_not_pending(self, tmp_path: Path) -> None:
        transcript = _write_transcript(tmp_path, [_ask_block("")])
        assert _pending_ask_text(str(transcript)) is None


class TestHandleNotifyStalledAsk:
    def test_dms_the_owner_with_session_cwd_and_question(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("T3_OVERLAY_NAME", "t3-teatree")
        transcript = _write_transcript(tmp_path, [_ask_block("Merge PR #1?")])
        with (
            patch.object(gate, "t3_argv", return_value=["/x/t3", "teatree", "notify", "send", "body"]) as argv,
            patch.object(gate, "spawn_t3_detached") as spawn,
        ):
            handle_notify_stalled_ask({"session_id": "s-1", "cwd": "/repo", "transcript_path": str(transcript)})

        assert spawn.call_count == 1
        args = argv.call_args.args
        assert args[0] == "teatree"  # the t3- prefix is stripped for the CLI group
        assert "notify" in args
        assert "send" in args
        assert "--idempotency-key" in args
        assert "question" in args  # --kind question
        body = args[args.index("send") + 1]
        assert "s-1" in body
        assert "/repo" in body
        assert "Merge PR #1?" in body

    def test_no_pending_ask_sends_nothing(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("T3_OVERLAY_NAME", "t3-teatree")
        transcript = _write_transcript(tmp_path, _answered("Merge PR #1?"))
        with patch.object(gate, "spawn_t3_detached") as spawn:
            handle_notify_stalled_ask({"session_id": "s-1", "transcript_path": str(transcript)})
        assert spawn.call_count == 0

    def test_no_overlay_configured_sends_nothing(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        transcript = _write_transcript(tmp_path, [_ask_block("Merge PR #1?")])
        with patch.object(gate, "spawn_t3_detached") as spawn:
            handle_notify_stalled_ask({"session_id": "s-1", "transcript_path": str(transcript)})
        assert spawn.call_count == 0

    def test_unresolvable_t3_argv_sends_nothing(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("T3_OVERLAY_NAME", "t3-teatree")
        transcript = _write_transcript(tmp_path, [_ask_block("Merge PR #1?")])
        with (
            patch.object(gate, "t3_argv", return_value=None),
            patch.object(gate, "spawn_t3_detached") as spawn,
        ):
            handle_notify_stalled_ask({"session_id": "s-1", "transcript_path": str(transcript)})
        assert spawn.call_count == 0

    def test_a_repeat_notification_for_the_same_ask_reuses_the_same_key(self, tmp_path: Path, monkeypatch) -> None:
        """No separate marker file needed.

        Repeat delivery suppression is delegated to ``notify send``'s own
        idempotency dedupe keyed on this stable value.
        """
        monkeypatch.setenv("T3_OVERLAY_NAME", "t3-teatree")
        transcript = _write_transcript(tmp_path, [_ask_block("Merge PR #1?")])
        keys = []
        with (
            patch.object(gate, "t3_argv", side_effect=lambda *a: list(a)) as argv,
            patch.object(gate, "spawn_t3_detached"),
        ):
            handle_notify_stalled_ask({"session_id": "s-1", "transcript_path": str(transcript)})
            handle_notify_stalled_ask({"session_id": "s-1", "transcript_path": str(transcript)})
            for call in argv.call_args_list:
                a = call.args
                keys.append(a[a.index("--idempotency-key") + 1])
        assert keys[0] == keys[1]

    def test_a_different_question_gets_a_different_key(self) -> None:
        """Control: the dedupe key is not blanket per-session."""
        key1 = _stalled_ask_idempotency_key("s-1", "Merge PR #1?")
        key2 = _stalled_ask_idempotency_key("s-1", "Merge PR #2?")
        assert key1 != key2

    def test_crash_in_the_t3_shell_out_is_swallowed(self, tmp_path: Path, monkeypatch) -> None:
        """Advisory-only: this hook can only ever add a DM, never surface an error."""
        monkeypatch.setenv("T3_OVERLAY_NAME", "t3-teatree")
        transcript = _write_transcript(tmp_path, [_ask_block("Merge PR #1?")])
        with (
            patch.object(gate, "t3_argv", return_value=["/x/t3", "notify", "send", "b", "--idempotency-key", "k"]),
            patch.object(gate, "spawn_t3_detached", side_effect=RuntimeError("boom")),
        ):
            handle_notify_stalled_ask({"session_id": "s-1", "transcript_path": str(transcript)})  # must not raise


class TestWiredIntoRouter:
    def test_registered_on_the_notification_event(self) -> None:
        assert router.handle_notify_stalled_ask in router._HANDLERS["Notification"]

    def test_reexport_is_the_same_object(self) -> None:
        assert router.handle_notify_stalled_ask is gate.handle_notify_stalled_ask
