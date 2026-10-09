"""Transcript parsing for the Stop gates — ``question_gates`` is its one home.

Six Stop surfaces read ``last_assistant_turn`` to decide what "this turn" said.
It used to end the turn at the first ``user`` entry, but the harness records a
tool RESULT as a ``user`` entry too, so any turn that called a tool — which is
every delegation-report turn — was silently truncated at its first tool call.
Everything the assistant wrote before dispatching fell outside the turn.
"""

import json
import time
from pathlib import Path

import pytest

from hooks.scripts import question_gates
from hooks.scripts.question_gates import is_tool_result_only, iter_transcript_reversed, last_assistant_turn


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


class TestTheTranscriptReadNewestFirst:
    _SHAPES = ("entry", "blank", "crlf", "entry", "garbage", "long", "multibyte")

    def _lines(self, seed: int) -> list[bytes]:
        lines: list[bytes] = []
        for index in range(seed % 13):
            shape = self._SHAPES[(seed * 7 + index * 3) % len(self._SHAPES)]
            width = (seed * 31 + index * 17) % 300
            if shape in {"entry", "long", "crlf"}:
                pad = "x" * (width if shape == "long" else width % 40)
                lines.append(json.dumps({"n": index, "pad": pad}).encode() + (b"\r" if shape == "crlf" else b""))
            elif shape == "multibyte":
                lines.append(json.dumps({"n": index, "pad": "é€" * (width % 40 + 1)}, ensure_ascii=False).encode())
            elif shape == "garbage":
                lines.append(b"{not json")
            else:
                lines.append(b"")
        return lines

    @pytest.mark.parametrize("block", [1, 2, 3, 7, 64, 65536])
    @pytest.mark.parametrize("seed", range(12))
    def test_yields_exactly_the_forward_parse_reversed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block: int, seed: int
    ) -> None:
        monkeypatch.setattr(question_gates, "_TAIL_BLOCK_BYTES", block)
        lines = self._lines(seed)
        path = tmp_path / "transcript.jsonl"
        path.write_bytes(b"\n".join(lines) + (b"\n" if seed % 2 else b""))
        forward = [json.loads(line) for line in lines if line.strip() and line != b"{not json"]

        assert list(iter_transcript_reversed(str(path))) == forward[::-1]

    def test_the_corpus_carries_every_shape(self) -> None:
        corpus = b"\n".join(line for seed in range(12) for line in self._lines(seed))

        assert b"\r" in corpus
        assert "€".encode() in corpus
        assert b"{not json" in corpus

    def test_one_huge_line_costs_no_more_than_the_same_bytes_in_short_lines(self, tmp_path: Path) -> None:
        size = 32 * 1024 * 1024
        head = json.dumps({"n": 0}).encode() + b"\n"
        huge, short = tmp_path / "huge.jsonl", tmp_path / "short.jsonl"
        huge.write_bytes(head + b"z" * size + b"\n")
        short.write_bytes(head + (b"z" * 63 + b"\n") * (size // 64))

        started = time.process_time()
        assert list(iter_transcript_reversed(str(short))) == [{"n": 0}]
        linear = time.process_time() - started
        started = time.process_time()
        assert list(iter_transcript_reversed(str(huge))) == [{"n": 0}]

        assert time.process_time() - started < 2 * linear
