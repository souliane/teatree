"""Tests for ``pending_ask_text``.

The shared shape both the Notification DM and the doctor finding key on (#4818) — a
trailing, still-blocking ``AskUserQuestion``.
"""

import json
from pathlib import Path

from teatree.hooks.stalled_ask_detect import pending_ask_text


def _ask_block(question: str) -> dict:
    tool_use = {"type": "tool_use", "name": "AskUserQuestion", "input": {"questions": [{"question": question}]}}
    return {"type": "assistant", "message": {"role": "assistant", "content": [tool_use]}}


def _write(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


class TestPendingAskText:
    def test_trailing_ask_is_found(self, tmp_path: Path) -> None:
        transcript = _write(tmp_path, [_ask_block("Approve the deploy?")])
        assert pending_ask_text(str(transcript)) == "Approve the deploy?"

    def test_a_multi_entry_transcript_reads_only_the_last(self, tmp_path: Path) -> None:
        older = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}}
        transcript = _write(tmp_path, [older, _ask_block("Approve the deploy?")])
        assert pending_ask_text(str(transcript)) == "Approve the deploy?"

    def test_answered_ask_is_not_pending(self, tmp_path: Path) -> None:
        result = {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "1"}]}}
        transcript = _write(tmp_path, [_ask_block("Approve the deploy?"), result])
        assert pending_ask_text(str(transcript)) is None

    def test_no_ask_in_the_trailing_turn_is_not_pending(self, tmp_path: Path) -> None:
        content = [{"type": "text", "text": "Done."}]
        text_only = {"type": "assistant", "message": {"role": "assistant", "content": content}}
        transcript = _write(tmp_path, [text_only])
        assert pending_ask_text(str(transcript)) is None

    def test_missing_transcript_is_not_pending(self, tmp_path: Path) -> None:
        assert pending_ask_text(str(tmp_path / "missing.jsonl")) is None

    def test_blank_transcript_path_is_not_pending(self) -> None:
        assert pending_ask_text("") is None

    def test_empty_question_text_is_not_pending(self, tmp_path: Path) -> None:
        transcript = _write(tmp_path, [_ask_block("")])
        assert pending_ask_text(str(transcript)) is None

    def test_trailing_blank_lines_are_skipped(self, tmp_path: Path) -> None:
        """The real writer may leave a trailing newline; the last NON-blank line wins."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(json.dumps(_ask_block("Approve?")) + "\n\n\n", encoding="utf-8")
        assert pending_ask_text(str(transcript)) == "Approve?"

    def test_malformed_trailing_line_falls_back_to_the_last_valid_one(self, tmp_path: Path) -> None:
        """A truncated final write (mid-flush) must not raise — skip it, don't crash."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(json.dumps(_ask_block("Approve?")) + "\n{not json", encoding="utf-8")
        assert pending_ask_text(str(transcript)) == "Approve?"
