"""Transcript parsing for the Stop gates — ``question_gates`` is its one home.

Six Stop surfaces read ``last_assistant_turn`` to decide what "this turn" said.
It used to end the turn at the first ``user`` entry, but the harness records a
tool RESULT as a ``user`` entry too, so any turn that called a tool — which is
every delegation-report turn — was silently truncated at its first tool call.
Everything the assistant wrote before dispatching fell outside the turn.
"""

import json
from pathlib import Path

from hooks.scripts.question_gates import is_tool_result_only, last_assistant_turn


def _assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _tool_result(tool_use_id: str = "toolu_01") -> dict:
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}
    return {"type": "user", "message": {"role": "user", "content": [block]}}


def _write(tmp_path: Path, entries: list[dict]) -> str:
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
    return str(path)


class TestIsToolResultOnly:
    def test_a_tool_result_entry_is_not_the_user_speaking(self) -> None:
        assert is_tool_result_only([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}])

    def test_a_typed_message_is_the_user_speaking(self) -> None:
        assert not is_tool_result_only([{"type": "text", "text": "why?"}])

    def test_a_mixed_entry_is_the_user_speaking(self) -> None:
        assert not is_tool_result_only([{"type": "tool_result", "tool_use_id": "t1"}, {"type": "text", "text": "and?"}])

    def test_an_empty_entry_is_not_a_tool_result(self) -> None:
        assert not is_tool_result_only([])


class TestLastAssistantTurn:
    def test_last_assistant_turn_spans_a_tool_result_boundary(self, tmp_path: Path) -> None:
        transcript = _write(
            tmp_path,
            [
                _user("why was it not merged?"),
                _assistant(
                    _text("Because the eval lane is red."), {"type": "tool_use", "id": "toolu_01", "name": "Task"}
                ),
                _tool_result(),
                _assistant(_text("Dispatched a lane to fix it.")),
            ],
        )

        turn = last_assistant_turn(transcript)

        assert turn is not None
        text, _used_question_tool = turn
        assert "Because the eval lane is red." in text
        assert "Dispatched a lane to fix it." in text

    def test_a_real_user_message_still_ends_the_turn(self, tmp_path: Path) -> None:
        transcript = _write(
            tmp_path,
            [_assistant(_text("an earlier turn")), _user("now do this"), _assistant(_text("the current turn"))],
        )

        turn = last_assistant_turn(transcript)

        assert turn is not None
        text, _used_question_tool = turn
        assert "an earlier turn" not in text
        assert "the current turn" in text

    def test_a_question_tool_used_before_a_tool_result_is_seen(self, tmp_path: Path) -> None:
        # The ALLOW widening the fix carries: #807 now sees an AskUserQuestion issued
        # before a tool result, where the truncated turn used to miss it and block a
        # turn that did ask through the structured tool.
        transcript = _write(
            tmp_path,
            [
                _user("which branch?"),
                _assistant({"type": "tool_use", "id": "toolu_01", "name": "AskUserQuestion"}),
                _tool_result(),
                _assistant(_text("Waiting on your pick.")),
            ],
        )

        turn = last_assistant_turn(transcript)

        assert turn is not None
        _text_out, used_question_tool = turn
        assert used_question_tool is True
