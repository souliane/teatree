# test-path: cross-cutting — tests hooks/scripts/turn_inspect.py, which has no src/teatree/ mirror.
"""Turn-boundary projections read by the consideration and closure-reverify Stop gates.

A tool RESULT is recorded as a ``user`` entry whose blocks are all ``tool_result``,
so a walk that stops at the FIRST ``user`` entry cuts the turn at the first tool
call — and every real tool-using turn then projects to nothing, leaving the
consideration gate inert and the closure-reverify gate unable to see the
verification command that clears it. These fixtures put a ``tool_result`` entry
between the tool use and the final assistant text, which is the shape the defect
lives in.
"""

import json
from pathlib import Path
from typing import Any

from hooks.scripts.turn_inspect import current_turn_assistant_text, current_turn_edits, current_turn_tool_commands


def _write_transcript(tmp_path: Path, entries: list[dict[str, Any]]) -> str:
    path = tmp_path / "transcript.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")
    return str(path)


def _user_turn(text: str = "do the thing") -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _tool_result(tool_use_id: str = "toolu_1") -> dict[str, Any]:
    """The entry the harness writes back after a tool runs — a ``user`` entry that is not the user."""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}],
        },
    }


def _assistant_tool_use(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "tool_use", "name": name, "input": tool_input}]},
    }


def _assistant_text(text: str) -> dict[str, Any]:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _tool_using_turn(tmp_path: Path) -> str:
    """The real shape of a tool-using turn: prompt, tool_use, tool_result, final text."""
    return _write_transcript(
        tmp_path,
        [
            _user_turn("earlier prompt"),
            _assistant_text("earlier answer"),
            _user_turn(),
            _assistant_tool_use("Edit", {"file_path": "/tmp/agent-settings.json", "new_string": "x"}),
            _tool_result(),
            _assistant_tool_use("Bash", {"command": "gh pr view 4217", "description": "check the PR"}),
            _tool_result("toolu_2"),
            _assistant_text("closed #4217"),
        ],
    )


class TestWalksPastToolResults:
    def test_edits_before_a_tool_result_are_seen(self, tmp_path: Path) -> None:
        assert current_turn_edits(_tool_using_turn(tmp_path)) == ["/tmp/agent-settings.json"]

    def test_tool_commands_before_a_tool_result_are_seen(self, tmp_path: Path) -> None:
        commands = current_turn_tool_commands(_tool_using_turn(tmp_path))
        assert "gh pr view 4217" in commands
        assert "check the PR" in commands

    def test_assistant_text_before_a_tool_result_is_seen(self, tmp_path: Path) -> None:
        text = current_turn_assistant_text(_tool_using_turn(tmp_path))
        assert "closed #4217" in text
        assert "earlier answer" not in text


class TestStopsAtTheGenuineUserBoundary:
    """The previous turn stays out — the boundary moved past tool results, not past the user."""

    def test_prior_turn_edit_is_excluded(self, tmp_path: Path) -> None:
        transcript = _write_transcript(
            tmp_path,
            [
                _user_turn("earlier prompt"),
                _assistant_tool_use("Write", {"file_path": "/tmp/prior-turn.txt", "content": "x"}),
                _user_turn(),
                _assistant_tool_use("Write", {"file_path": "/tmp/this-turn.txt", "content": "x"}),
                _tool_result(),
                _assistant_text("done"),
            ],
        )
        assert current_turn_edits(transcript) == ["/tmp/this-turn.txt"]

    def test_prior_turn_command_is_excluded(self, tmp_path: Path) -> None:
        transcript = _write_transcript(
            tmp_path,
            [
                _user_turn("earlier prompt"),
                _assistant_tool_use("Bash", {"command": "gh pr view 1"}),
                _user_turn(),
                _assistant_tool_use("Bash", {"command": "gh pr view 2"}),
                _tool_result(),
                _assistant_text("done"),
            ],
        )
        assert current_turn_tool_commands(transcript) == ["gh pr view 2"]


class TestDegradedInputs:
    def test_missing_transcript_yields_empty(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "absent.jsonl")
        assert current_turn_edits(missing) == []
        assert current_turn_tool_commands(missing) == []
        assert current_turn_assistant_text(missing) == ""

    def test_empty_user_content_still_ends_the_turn(self, tmp_path: Path) -> None:
        """An empty-content user entry is not a tool result, so it stays a boundary."""
        transcript = _write_transcript(
            tmp_path,
            [
                _assistant_tool_use("Write", {"file_path": "/tmp/prior.txt", "content": "x"}),
                {"type": "user", "message": {"role": "user", "content": []}},
                _assistant_text("done"),
            ],
        )
        assert current_turn_edits(transcript) == []
