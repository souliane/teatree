"""Tests for the completion-claim Stop gate (issue #2665).

When an assistant turn ends with a HIGH-confidence completeness assertion on a
multi-deliverable ticket and no complete on-target deliverable->evidence map,
the Stop hook blocks: it returns ``{"decision": "block", "reason": ...}``
instructing the agent to produce the map before claiming done. Persisting the
verification-before-completion rule as prose (and the WARN-only closure-reverify
advisory) has not prevented recurrence — only a non-bypassable hook does.

Precision posture mirrors the structured-question gate: it fires only on a
loop-driven turn, short-circuits the ``stop_hook_active`` re-fire, honours the
``[skip-completion-gate: <reason>]`` token and the kill-switch, and the detector
is tuned so a legitimate single-deliverable "done" or a complete on-target map
is never blocked.

Integration-style: the real ``hook_router`` handler, a real transcript JSONL
written under ``tmp_path``; only stdin/stdout cross the handler boundary.
"""

import json
from pathlib import Path

import pytest

import hooks.scripts.completion_claim_gate as gate
import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_completion_claim_gate

# A multi-deliverable completion claim with no on-target evidence map — the real
# incident shape. Must BLOCK.
_STRANDED_CLAIM = (
    "Reviewed all the open MRs — no blockers anywhere.\n"
    "- Backend change: MR opened.\n"
    "- Authoring UI: MR opened.\n"
    "- Frontend banner: MR opened.\n"
    "Everything is here and ready to merge.\n"
)

# A complete on-target deliverable->evidence map. Must NOT block.
_COMPLETE_MAP = (
    "I read the authoritative spec and its comments and enumerated every deliverable.\n"
    "- Backend serializer change: merged to the merge target, verified on main.\n"
    "- Crucial deliverable (the authoring UI): verified on the correct config surface.\n"
    "- Frontend banner: passing E2E, evidence posted.\n"
    "All deliverables are done on the merge target.\n"
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


@pytest.fixture(autouse=True)
def _force_loop_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    # The gate only fires on a loop-driven turn; force the driver verdict so the
    # block-path tests exercise the gate (an attended turn would skip it).
    monkeypatch.setattr(router, "_session_drives_loop", lambda _session_id: True)


class TestBlocksUnbackedMultiDeliverableClaim:
    """Anti-vacuous: a multi-deliverable 'done' with no on-target evidence map."""

    def test_stranded_claim_blocks(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        transcript = _write_transcript(tmp_path, [_user("deliver the ticket"), _assistant(_STRANDED_CLAIM)])

        result = handle_completion_claim_gate({"transcript_path": str(transcript)})

        decision = _decision(capsys)
        assert decision.get("decision") == "block"
        assert "COMPLETION-CLAIM GATE (#2665)" in decision.get("reason", "")
        assert "NOT done" in decision.get("reason", "")
        assert result is True


class TestPassesWhenCompliantOrOutOfScope:
    def test_complete_on_target_map_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        transcript = _write_transcript(tmp_path, [_user("deliver the ticket"), _assistant(_COMPLETE_MAP)])

        result = handle_completion_claim_gate({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_single_deliverable_done_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        body = "Fixed the typo in the README.\n- Updated the heading.\nDone."
        transcript = _write_transcript(tmp_path, [_user("fix the typo"), _assistant(body)])

        result = handle_completion_claim_gate({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_honest_refusal_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        body = (
            "- Backend change: merged to target.\n"
            "- Authoring UI: MR opened.\n"
            "NOT done: the authoring UI is on the wrong surface and stranded off target.\n"
        )
        transcript = _write_transcript(tmp_path, [_user("deliver"), _assistant(body)])

        result = handle_completion_claim_gate({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_attended_non_driver_turn_is_skipped(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A different live session drives the loop → this is the attended turn a
        # human reads, so the gate must NOT fire even on a stranded claim.
        monkeypatch.setattr(router, "_session_drives_loop", lambda _session_id: False)
        transcript = _write_transcript(tmp_path, [_user("deliver"), _assistant(_STRANDED_CLAIM)])

        result = handle_completion_claim_gate({"session_id": "s", "transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True


class TestEscapesAndRefireGuard:
    def test_skip_token_in_turn_text_allows(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        body = _STRANDED_CLAIM + "\n[skip-completion-gate: single doc deliverable, map not applicable]"
        transcript = _write_transcript(tmp_path, [_user("deliver"), _assistant(body)])

        result = handle_completion_claim_gate({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_kill_switch_disables_gate(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gate, "_completion_claim_gate_enabled", lambda: False)
        transcript = _write_transcript(tmp_path, [_user("deliver"), _assistant(_STRANDED_CLAIM)])

        result = handle_completion_claim_gate({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_stop_hook_active_refire_is_noop(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        transcript = _write_transcript(tmp_path, [_user("deliver"), _assistant(_STRANDED_CLAIM)])

        result = handle_completion_claim_gate({"transcript_path": str(transcript), "stop_hook_active": True})

        assert _decision(capsys) == {}
        assert result is not True


class TestFailSafeAndEdgeInputs:
    def test_missing_transcript_path_is_noop(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_completion_claim_gate({})
        assert _decision(capsys) == {}
        assert result is not True

    def test_nonexistent_transcript_file_is_noop(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_completion_claim_gate({"transcript_path": str(tmp_path / "nope.jsonl")})
        assert _decision(capsys) == {}
        assert result is not True

    def test_malformed_transcript_lines_are_noop(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = tmp_path / "t.jsonl"
        path.write_text("{not json\n{}\n", encoding="utf-8")
        result = handle_completion_claim_gate({"transcript_path": str(path)})
        assert _decision(capsys) == {}
        assert result is not True
