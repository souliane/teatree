"""Tests for the answer-first Stop gate — the inverse of the structured-question gate.

The structured-question gate (#807) fires when the AGENT poses a question without
the structured tool. Nothing fired on the inverse: the USER asks, and the agent
replies with a delegation report instead of an answer, so the owner has to ask
the same thing twice. This gate closes that asymmetry — it blocks turn-end when
the last user message asks something answerable and the final assistant turn
reports a dispatch carrying no answer.

Integration-style, matching the sibling Stop-gate tests: the real handler, a real
transcript JSONL under ``tmp_path``, only stdin/stdout crossing the boundary.
"""

import json
from pathlib import Path

import pytest

import hooks.scripts.answer_first_gate as gate
import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_answer_first_gate

_QUESTION = "are you going to merge 4001 or not??"
_DISPATCH_ONLY = "Dispatched a lane to merge #4001. It is queued behind the current tick.\n"
_ANSWERED = "Yes — merging it now. Dispatched a lane to run the merge.\n"


def _assistant(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _write_transcript(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def _decision(capsys: pytest.CaptureFixture[str]) -> dict:
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else {}


class TestBlocksActionInsteadOfAnswer:
    def test_dispatch_report_answering_a_question_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        transcript = _write_transcript(tmp_path, [_user(_QUESTION), _assistant(_DISPATCH_ONLY)])

        result = handle_answer_first_gate({"transcript_path": str(transcript)})

        decision = _decision(capsys)
        assert decision.get("decision") == "block"
        assert "ANSWER-FIRST GATE" in decision.get("reason", "")
        assert result is True


class TestPassesWhenAnsweredOrOutOfScope:
    def test_answered_question_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        transcript = _write_transcript(tmp_path, [_user(_QUESTION), _assistant(_ANSWERED)])

        result = handle_answer_first_gate({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_stop_hook_re_fire_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        transcript = _write_transcript(tmp_path, [_user(_QUESTION), _assistant(_DISPATCH_ONLY)])

        result = handle_answer_first_gate({"transcript_path": str(transcript), "stop_hook_active": True})

        assert _decision(capsys) == {}
        assert result is not True

    def test_skip_token_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        body = _DISPATCH_ONLY + "[skip-answer-gate: the answer is in the posted table above]\n"
        transcript = _write_transcript(tmp_path, [_user(_QUESTION), _assistant(body)])

        result = handle_answer_first_gate({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_kill_switch_passes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gate, "_gate_enabled", lambda: False)
        transcript = _write_transcript(tmp_path, [_user(_QUESTION), _assistant(_DISPATCH_ONLY)])

        result = handle_answer_first_gate({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_missing_transcript_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_answer_first_gate({"transcript_path": str(tmp_path / "absent.jsonl")})

        assert _decision(capsys) == {}
        assert result is not True


class TestInjectedContextIsNotTheUserSpeaking:
    """The agent's own pending questions ride into the next user turn — and read as asks."""

    _BACKLOG = (
        "continue the tick\n"
        "You have 2 deferred question(s) awaiting user answer:\n"
        "  #12 — Do you want me to merge PR #41 now?\n"
        "  #13 — Which region should this deploy to?\n"
    )

    def test_injected_deferred_backlog_does_not_block(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        transcript = _write_transcript(tmp_path, [_user(self._BACKLOG), _assistant(_DISPATCH_ONLY)])

        result = handle_answer_first_gate({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_control_the_same_question_typed_by_the_user_does_block(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        transcript = _write_transcript(
            tmp_path, [_user("Do you want me to merge PR #41 now?"), _assistant(_DISPATCH_ONLY)]
        )

        result = handle_answer_first_gate({"transcript_path": str(transcript)})

        assert _decision(capsys).get("decision") == "block"
        assert result is True

    def test_harness_ambient_wrapper_is_not_the_user_speaking(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body = "continue the tick\n<system-reminder>\nDid you remember to run the suite?\n</system-reminder>\n"
        transcript = _write_transcript(tmp_path, [_user(body), _assistant(_DISPATCH_ONLY)])

        result = handle_answer_first_gate({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True


class TestRegisteredOnTheStopChain:
    def test_handler_runs_before_the_self_pump(self) -> None:
        names = [handler.__name__ for handler in router._HANDLERS["Stop"]]

        assert "handle_answer_first_gate" in names
        assert names.index("handle_answer_first_gate") < names.index("handle_loop_self_pump")
