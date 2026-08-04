"""Tests for the unbacked-claim Stop gate — a diagnosis or an alarm cites what was read.

The completion-claim gate demands per-deliverable evidence for a DONE claim. The
same principle was missing one step earlier, for the DIAGNOSTIC claim: "CI failed
because X" needs the log line, and a severity escalation needs the artefact that
establishes it rather than a relayed symptom with the settling evidence still
outstanding.

Integration-style, matching the sibling Stop-gate tests: the real handler, a real
transcript JSONL under ``tmp_path``, only stdin/stdout crossing the boundary.
"""

import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.unbacked_claim_gate as gate
from hooks.scripts.hook_router import handle_unbacked_claim_gate

_FLUENT_DIAGNOSIS = "#4001 failed because 466 files were meeting upstream's gates for the first time.\n"
_CITED_DIAGNOSIS = (
    "#4001 failed because of one real violation it introduced:\n"
    "```\nsrc/teatree/core/models/ticket.py: 523 LOC, up from 510 (over the 500 cap).\n```\n"
)
_PREMATURE_ALARM = (
    "SEVERE: a selected offer repriced 37.5 bps between quote and acceptance.\n"
    "I am awaiting the lane's report on the offer ordering before I can confirm.\n"
)


def _assistant(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _user(text: str = "go") -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _write_transcript(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def _decision(capsys: pytest.CaptureFixture[str]) -> dict:
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else {}


class TestBlocksUnbackedClaims:
    def test_uncited_diagnosis_blocks(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        transcript = _write_transcript(tmp_path, [_user(), _assistant(_FLUENT_DIAGNOSIS)])

        result = handle_unbacked_claim_gate({"transcript_path": str(transcript)})

        decision = _decision(capsys)
        assert decision.get("decision") == "block"
        assert "EVIDENCE GATE" in decision.get("reason", "")
        assert result is True

    def test_premature_alarm_blocks(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        transcript = _write_transcript(tmp_path, [_user(), _assistant(_PREMATURE_ALARM)])

        result = handle_unbacked_claim_gate({"transcript_path": str(transcript)})

        decision = _decision(capsys)
        assert decision.get("decision") == "block"
        assert "outstanding" in decision.get("reason", "")
        assert result is True


class TestPassesWhenCitedOrOutOfScope:
    def test_cited_diagnosis_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        transcript = _write_transcript(tmp_path, [_user(), _assistant(_CITED_DIAGNOSIS)])

        result = handle_unbacked_claim_gate({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_stop_hook_re_fire_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        transcript = _write_transcript(tmp_path, [_user(), _assistant(_FLUENT_DIAGNOSIS)])

        result = handle_unbacked_claim_gate({"transcript_path": str(transcript), "stop_hook_active": True})

        assert _decision(capsys) == {}
        assert result is not True

    def test_skip_token_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        body = _FLUENT_DIAGNOSIS + "[skip-evidence-gate: the log is in the linked job, quoted upthread]\n"
        transcript = _write_transcript(tmp_path, [_user(), _assistant(body)])

        result = handle_unbacked_claim_gate({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_kill_switch_passes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gate, "_gate_enabled", lambda: False)
        transcript = _write_transcript(tmp_path, [_user(), _assistant(_FLUENT_DIAGNOSIS)])

        result = handle_unbacked_claim_gate({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_missing_transcript_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_unbacked_claim_gate({"transcript_path": str(tmp_path / "absent.jsonl")})

        assert _decision(capsys) == {}
        assert result is not True


class TestRegisteredOnTheStopChain:
    def test_handler_runs_before_the_self_pump(self) -> None:
        names = [handler.__name__ for handler in router._HANDLERS["Stop"]]

        assert "handle_unbacked_claim_gate" in names
        assert names.index("handle_unbacked_claim_gate") < names.index("handle_loop_self_pump")
