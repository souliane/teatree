"""Tests for the structured-question Stop gate (issue #807).

When an assistant turn ends with a user-directed question posed *inline in
prose* and no structured-question (``AskUserQuestion``) tool call happened
in that same turn, the Stop hook blocks: it returns
``{"decision": "block", "reason": ...}`` instructing the agent to re-ask
through the structured question tool. Persisting "ask via the structured
tool" as a soft memory has not changed behaviour — only a non-bypassable
hook does. There is intentionally no ``relax:`` escape (it is a gate, like
the other Stop-time gates in ``hook_router.py``).

Detection heuristic (tuned for precision, documented on the handler):
final assistant text has ``?`` *and* a second-person/decision cue, *and*
no ``AskUserQuestion`` tool_use occurred in the last assistant turn, *and*
it is not an already-answered/denied case.

Integration-style: real ``hook_router`` handler, a real transcript JSONL
written under ``tmp_path``; only stdin/stdout are exercised through the
handler.
"""

import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_enforce_structured_question


def _assistant(text: str, tool_uses: list[str] | None = None) -> dict:
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    content.extend({"type": "tool_use", "name": name, "input": {}} for name in tool_uses or [])
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def _user(text: str = "go") -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _write_transcript(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def _decision(capsys: pytest.CaptureFixture[str]) -> dict:
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else {}


class TestBlocksInlineUserDirectedQuestion:
    """Anti-vacuous: an inline user-directed question with no tool call.

    Without the hook the import fails and these go RED; with it they
    assert a ``block`` decision (a semantic, not structural, guard).
    """

    @pytest.mark.parametrize(
        "question",
        [
            "Should I push the branch now, or wait for review?",
            "Do you want me to open the PR?",
            "Which approach do you prefer — A or B?",
            "Let me know if you'd like me to proceed.",
            "Shall I merge this once CI is green?",
            "I can do X or Y — which would you like?",
        ],
    )
    def test_inline_question_without_tool_call_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], question: str
    ) -> None:
        transcript = _write_transcript(
            tmp_path,
            [_user("implement the feature"), _assistant(f"I finished the work. {question}")],
        )

        result = handle_enforce_structured_question({"transcript_path": str(transcript)})

        decision = _decision(capsys)
        assert decision.get("decision") == "block"
        assert "AskUserQuestion" in decision.get("reason", "")
        assert result is True

    def test_question_buried_in_long_status_message_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status = (
            "Coordinator status:\n- ticket #1 shipped\n- ticket #2 in review\n"
            "- pipeline green on #3\nEverything is progressing.\n\n"
            "One open point: should I bundle the follow-up into the current PR?"
        )
        transcript = _write_transcript(tmp_path, [_user("run the loop"), _assistant(status)])

        result = handle_enforce_structured_question({"transcript_path": str(transcript)})

        assert _decision(capsys).get("decision") == "block"
        assert result is True


class TestPassesWhenCompliantOrNoQuestion:
    def test_question_with_askuserquestion_tool_call_passes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        transcript = _write_transcript(
            tmp_path,
            [
                _user("implement the feature"),
                _assistant("Which approach do you prefer?", tool_uses=["AskUserQuestion"]),
            ],
        )

        result = handle_enforce_structured_question({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_no_question_status_sentence_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        transcript = _write_transcript(
            tmp_path,
            [_user("status?"), _assistant("All three tickets are shipped and pipelines are green.")],
        )

        result = handle_enforce_structured_question({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_rhetorical_question_without_decision_cue_passes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A '?' with no second-person/decision cue — e.g. echoing the user's
        # own phrasing or a rhetorical aside — must not trip the gate.
        transcript = _write_transcript(
            tmp_path,
            [_user("why did it fail?"), _assistant("The build failed because the lockfile was stale. Fixed it.")],
        )

        result = handle_enforce_structured_question({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_question_in_earlier_turn_not_final_turn_passes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The decision cue is in a PRIOR assistant turn; the final turn is a
        # plain status. Only the last turn is judged.
        transcript = _write_transcript(
            tmp_path,
            [
                _user("do it"),
                _assistant("Should I proceed?", tool_uses=["AskUserQuestion"]),
                _user("yes"),
                _assistant("Done. Implemented and committed."),
            ],
        )

        result = handle_enforce_structured_question({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_codeblock_only_question_mark_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # A '?' that only appears inside a fenced code block (e.g. a regex or
        # shell glob) is not a user-directed question.
        body = "Here is the fix:\n\n```python\nre.match(r'a?b', s)\n```\n\nDone."
        transcript = _write_transcript(tmp_path, [_user("fix it"), _assistant(body)])

        result = handle_enforce_structured_question({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True


class TestFailSafeAndEdgeInputs:
    def test_missing_transcript_path_is_noop(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_enforce_structured_question({})
        assert _decision(capsys) == {}
        assert result is not True

    def test_nonexistent_transcript_file_is_noop(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_enforce_structured_question({"transcript_path": str(tmp_path / "nope.jsonl")})
        assert _decision(capsys) == {}
        assert result is not True

    def test_malformed_transcript_lines_are_noop(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = tmp_path / "t.jsonl"
        path.write_text("{not json\n{}\n", encoding="utf-8")
        result = handle_enforce_structured_question({"transcript_path": str(path)})
        assert _decision(capsys) == {}
        assert result is not True

    def test_empty_transcript_is_noop(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = tmp_path / "t.jsonl"
        path.write_text("", encoding="utf-8")
        result = handle_enforce_structured_question({"transcript_path": str(path)})
        assert _decision(capsys) == {}
        assert result is not True

    def test_blank_lines_and_non_dict_json_lines_are_skipped(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Blank lines and a valid-JSON-but-not-dict line (a list) are
        # tolerated; the trailing real assistant turn still gates.
        path = tmp_path / "t.jsonl"
        path.write_text(
            "\n"
            + "[1, 2, 3]\n"
            + json.dumps(_user("do it"))
            + "\n\n"
            + json.dumps(_assistant("Done — should I push it now?"))
            + "\n",
            encoding="utf-8",
        )

        result = handle_enforce_structured_question({"transcript_path": str(path)})

        assert _decision(capsys).get("decision") == "block"
        assert result is True

    def test_unreadable_transcript_is_noop(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A file that exists but raises OSError on read (e.g. permissions)
        # must fail safe to "do nothing", not crash the Stop hook.
        path = tmp_path / "t.jsonl"
        path.write_text(json.dumps(_assistant("Should I proceed?")) + "\n", encoding="utf-8")

        def _boom(_self: Path, *_args: object, **_kwargs: object) -> str:
            msg = "unreadable"
            raise OSError(msg)

        monkeypatch.setattr(Path, "read_text", _boom)

        result = handle_enforce_structured_question({"transcript_path": str(path)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_non_dict_content_blocks_and_other_tool_use_are_skipped(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A non-dict content block and a non-AskUserQuestion tool_use must
        # neither crash nor count as a structured question; the inline
        # decision question in the same turn still gates.
        entry = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    "a bare string block",
                    {"type": "tool_use", "name": "Bash", "input": {}},
                    {"type": "text", "text": "All set. Want me to open the PR?"},
                ],
            },
        }
        path = tmp_path / "t.jsonl"
        path.write_text(json.dumps(_user("go")) + "\n" + json.dumps(entry) + "\n", encoding="utf-8")

        result = handle_enforce_structured_question({"transcript_path": str(path)})

        assert _decision(capsys).get("decision") == "block"
        assert result is True

    def test_turn_with_only_tool_use_no_text_is_noop(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # Final assistant turn has only a non-question tool_use, no text
        # block — nothing to judge, the session may end.
        transcript = _write_transcript(
            tmp_path,
            [_user("go"), _assistant("", tool_uses=["Bash"])],
        )

        result = handle_enforce_structured_question({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_stop_hook_active_guard_prevents_block_loop(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # If the Stop hook is already re-firing from its own block, do not
        # block again (Claude Code sets stop_hook_active) — avoid a hard loop.
        transcript = _write_transcript(
            tmp_path,
            [_user("go"), _assistant("Should I proceed with the deploy?")],
        )

        result = handle_enforce_structured_question({"transcript_path": str(transcript), "stop_hook_active": True})

        assert _decision(capsys) == {}
        assert result is not True


class TestWiredIntoRouter:
    def test_stop_event_includes_structured_question_gate(self) -> None:
        assert handle_enforce_structured_question in router._HANDLERS["Stop"]

    def test_runs_before_loop_self_pump(self) -> None:
        # The correctness gate must win the single-stdout slot: it is
        # registered before handle_loop_self_pump in the Stop chain.
        stop_chain = router._HANDLERS["Stop"]
        assert stop_chain.index(handle_enforce_structured_question) < stop_chain.index(router.handle_loop_self_pump)
