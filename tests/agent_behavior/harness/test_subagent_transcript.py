"""Adapt an in-session sub-agent JSONL into a gradeable :class:`EvalRun`."""

import json
from pathlib import Path

from teatree.eval.models import EvalSpec, Matcher
from teatree.eval.report import evaluate
from teatree.eval.subagent_transcript import is_subagent_transcript, subagent_run


def _spec() -> EvalSpec:
    return EvalSpec(
        name="worktree_first",
        scenario="s",
        agent_path="skills/code/SKILL.md",
        prompt="Fix README typo in the canonical clone.",
        matchers=(
            Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="git worktree add"),
        ),
        source_path=Path("spec.yaml"),
    )


def _assistant(content: list[dict], *, stop: str | None = None) -> str:
    """A session-schema assistant line; ``stop=None`` models the on-disk ``null`` stop_reason."""
    return json.dumps(
        {
            "isSidechain": True,
            "agentId": "agent-deadbeef",
            "type": "assistant",
            "message": {"role": "assistant", "content": content, "stop_reason": stop},
        }
    )


def _bash(command: str, tid: str) -> dict:
    return {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": command}, "caller": {}}


def _subagent_jsonl(*lines: str) -> str:
    return "\n".join(lines) + "\n"


class TestIsSubagentTranscript:
    def test_detects_session_envelope_keys(self) -> None:
        raw = _subagent_jsonl(_assistant([{"type": "text", "text": "hi"}]))
        assert is_subagent_transcript(raw)

    def test_rejects_stream_json_shape(self) -> None:
        stream = "\n".join(
            (
                json.dumps({"type": "system", "subtype": "init"}),
                json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}),
                json.dumps({"type": "result", "subtype": "success", "is_error": False}),
            )
        )
        assert not is_subagent_transcript(stream)

    def test_empty_is_not_subagent(self) -> None:
        assert not is_subagent_transcript("")

    def test_skips_malformed_lines_to_first_well_formed_object(self) -> None:
        raw = "not json\n" + _assistant([{"type": "text", "text": "hi"}]) + "\n"
        assert is_subagent_transcript(raw)

    def test_skips_blank_lines_then_detects(self) -> None:
        raw = "\n   \n" + _assistant([{"type": "text", "text": "hi"}]) + "\n"
        assert is_subagent_transcript(raw)

    def test_non_dict_first_object_is_skipped(self) -> None:
        raw = "[1, 2, 3]\n" + _assistant([{"type": "text", "text": "hi"}]) + "\n"
        assert is_subagent_transcript(raw)


class TestSubagentRun:
    def test_extracts_tool_calls_from_session_blocks(self) -> None:
        raw = _subagent_jsonl(
            _assistant([{"type": "text", "text": "Creating an isolated worktree first."}]),
            _assistant([_bash("git worktree add ../wt -b fix/typo", "t1")]),
        )
        run = subagent_run(_spec(), raw)
        assert [c.name for c in run.tool_calls] == ["Bash"]
        assert "git worktree add" in run.tool_calls[0].input["command"]

    def test_null_stop_reason_is_a_clean_completion(self) -> None:
        # The on-disk reality: the persisted stop_reason is null, not "end_turn".
        raw = _subagent_jsonl(_assistant([{"type": "text", "text": "done"}], stop=None))
        run = subagent_run(_spec(), raw)
        assert run.terminal_reason == "completed"
        assert not run.is_error

    def test_clean_string_stop_reason_is_not_an_error(self) -> None:
        raw = _subagent_jsonl(_assistant([{"type": "text", "text": "done"}], stop="end_turn"))
        run = subagent_run(_spec(), raw)
        assert run.terminal_reason == "end_turn"
        assert not run.is_error

    def test_dirty_stop_reason_marks_an_error(self) -> None:
        raw = _subagent_jsonl(_assistant([{"type": "text", "text": "truncated"}], stop="max_tokens"))
        run = subagent_run(_spec(), raw)
        assert run.terminal_reason == "max_tokens"
        assert run.is_error

    def test_no_assistant_event_is_an_abort(self) -> None:
        raw = _subagent_jsonl(
            json.dumps({"isSidechain": True, "type": "user", "message": {"role": "user", "content": "hi"}})
        )
        run = subagent_run(_spec(), raw)
        assert run.terminal_reason == "aborted"
        assert run.is_error

    def test_grades_to_real_pass_not_skip(self) -> None:
        raw = _subagent_jsonl(
            _assistant([_bash("git worktree add ../wt -b fix/typo", "t1")]),
            _assistant([_bash("sed -i '3s/Teatree/TeaTree/' ../wt/README.md", "t2")]),
        )
        result = evaluate(_spec(), subagent_run(_spec(), raw))
        assert not result.skipped
        assert result.passed

    def test_grades_to_real_fail_when_behavior_absent(self) -> None:
        raw = _subagent_jsonl(_assistant([_bash("sed -i '3s/Teatree/TeaTree/' README.md", "t1")]))
        result = evaluate(_spec(), subagent_run(_spec(), raw))
        assert not result.skipped
        assert not result.passed

    def test_tolerates_blank_malformed_and_typeless_lines(self) -> None:
        # Fail-soft: blank, non-JSON, non-dict, and a typeless object are all
        # skipped; the real tool call still extracts.
        raw = _subagent_jsonl(
            "",
            "not-json{",
            "[1, 2]",
            json.dumps({"isSidechain": True, "message": {"content": []}}),  # no "type"
            _assistant([_bash("git worktree add ../wt -b fix/typo", "t1")]),
        )
        run = subagent_run(_spec(), raw)
        assert [c.name for c in run.tool_calls] == ["Bash"]
        assert run.terminal_reason != "aborted"
