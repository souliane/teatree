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


def _tool_call(text: str, tool_use_id: str = "toolu_01") -> dict:
    content = [{"type": "text", "text": text}, {"type": "tool_use", "id": tool_use_id, "name": "Task", "input": {}}]
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def _tool_result(output: str = "agent dispatched", tool_use_id: str = "toolu_01") -> dict:
    """A tool result — recorded by the harness as a ``user`` entry, not as the user."""
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": output}
    return {"type": "user", "message": {"role": "user", "content": [block]}}


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


class TestSeesTheQuestionBehindAToolCall:
    """The regression that made the gate inert: a tool RESULT is a ``user`` entry.

    Every delegation-report turn calls a tool to do the delegating, and the
    harness records each result as ``type: "user"`` with ``tool_result`` blocks.
    A walk that stops at the first ``user`` entry therefore reads the tool result
    as the last user message, finds no text in it, and hands the detector an
    empty question — so the gate could only ever fire on a turn that CLAIMED a
    dispatch without making one, the one case that does not matter.
    """

    def test_question_before_a_tool_call_still_blocks(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        transcript = _write_transcript(
            tmp_path,
            [_user(_QUESTION), _tool_call("Let me get a lane on it."), _tool_result(), _assistant(_DISPATCH_ONLY)],
        )

        result = handle_answer_first_gate({"transcript_path": str(transcript)})

        decision = _decision(capsys)
        assert decision.get("decision") == "block"
        assert "or not" in decision.get("reason", "")
        assert result is True

    def test_question_behind_several_tool_results_still_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        transcript = _write_transcript(
            tmp_path,
            [
                _user(_QUESTION),
                _tool_call("Reading the ticket.", "toolu_01"),
                _tool_result("ticket #4001", "toolu_01"),
                _tool_call("Now dispatching.", "toolu_02"),
                _tool_result("agent started", "toolu_02"),
                _assistant(_DISPATCH_ONLY),
            ],
        )

        result = handle_answer_first_gate({"transcript_path": str(transcript)})

        assert _decision(capsys).get("decision") == "block"
        assert result is True

    def test_answered_question_behind_a_tool_call_passes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        transcript = _write_transcript(
            tmp_path, [_user(_QUESTION), _tool_call("On it."), _tool_result(), _assistant(_ANSWERED)]
        )

        result = handle_answer_first_gate({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True


class TestPoliteImperativesAreNotQuestions:
    """A polite imperative asks for the WORK — a dispatch report answers it.

    The owner writes in exactly that register ("can you merge this?"), so a
    modal second person on its own is courtesy, not an interrogative.
    """

    @pytest.mark.parametrize(
        "ask",
        [
            "can you merge 4001?",
            "could you rebase this and push?",
            "will you clean up the stale worktrees?",
            "would you take a look at the queue?",
        ],
    )
    def test_polite_imperative_does_not_block(
        self, ask: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        transcript = _write_transcript(tmp_path, [_user(ask), _tool_result(), _assistant(_DISPATCH_ONLY)])

        result = handle_answer_first_gate({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    @pytest.mark.parametrize(
        "ask",
        [
            "why did 4001 fail?",
            "can you tell me why it was not mergeable?",
            "could you confirm the pipeline is green?",
            "do you want me to merge PR #41 now?",
            "will you merge it or not?",
        ],
    )
    def test_genuine_interrogative_still_blocks(
        self, ask: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        transcript = _write_transcript(tmp_path, [_user(ask), _tool_result(), _assistant(_DISPATCH_ONLY)])

        result = handle_answer_first_gate({"transcript_path": str(transcript)})

        assert _decision(capsys).get("decision") == "block"
        assert result is True


class TestPastedMaterialIsNotTheUserAsking:
    def test_question_inside_a_quoted_paste_does_not_block(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body = "apply this review comment please\n> why was this not caught by the gate?\n> please fix it\n"
        transcript = _write_transcript(tmp_path, [_user(body), _assistant(_DISPATCH_ONLY)])

        result = handle_answer_first_gate({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_question_inside_a_fenced_paste_does_not_block(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body = "add this to the docs\n```\nQ: why did the run go red?\n```\n"
        transcript = _write_transcript(tmp_path, [_user(body), _assistant(_DISPATCH_ONLY)])

        result = handle_answer_first_gate({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True


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
