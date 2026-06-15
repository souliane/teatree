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
import os
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_enforce_structured_question, handle_warn_batched_questions


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


class TestClassifierRelaxExemption:
    """Stop gate must NOT block classifier-relax Step-2 explanation turns.

    The sanctioned Classifier Denial Protocol (skills/rules/SKILL.md §
    "Classifier Denial Protocol") requires the agent at Step 2 to explain
    the denial in plain text — naming the denied command, the goal, and the
    smallest permission rule — BEFORE calling AskUserQuestion at Step 3.
    That Step-2 prose contains second-person/decision cues and no
    AskUserQuestion tool call in the same turn, which is exactly the pattern
    the gate blocks. Without an exemption the agent is forced into an
    infinite loop: block → explain again → block.

    The exemption is narrow: only turns whose text contains a recognisable
    set of classifier-denial protocol markers are let through.
    """

    def test_step2_explanation_prose_is_not_blocked(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # Canonical Step-2 explanation: names the denied command, states the
        # goal, gives the smallest permission rule, poses the two options.
        # No AskUserQuestion tool call yet — that happens at Step 3.
        step2 = (
            "The command `gh issue create *` was denied by the classifier. "
            "I was trying to file a GitHub issue for the current ticket. "
            "The smallest static rule that would allow it is `Bash(gh issue create *)`. "
            "Should I add `Bash(gh issue create *)` to your `~/.claude/settings.json` "
            "permissions.allow? — Allow it (relax classifier) / Keep the denial (do it differently)."
        )
        transcript = _write_transcript(tmp_path, [_user("file the issue"), _assistant(step2)])

        result = handle_enforce_structured_question({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_step2_with_settings_json_mention_is_not_blocked(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Minimal: just mentions the settings.json edit target and "relax classifier".
        step2 = (
            "Bash(docker buildx prune *) was denied. "
            "I'll add the rule to ~/.claude/settings.json — relax classifier? "
            "Allow it (relax classifier) or keep the denial."
        )
        transcript = _write_transcript(tmp_path, [_user("clean up docker"), _assistant(step2)])

        result = handle_enforce_structured_question({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_step2_with_permissions_allow_mention_is_not_blocked(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Contains "permissions.allow" — the array the protocol names.
        step2 = (
            "The MCP call was denied. I was trying to fetch the ticket. "
            "The smallest rule is `Bash(gh api repos/*/issues*)`. "
            "I can add it to the permissions.allow array. "
            "Which would you prefer: Allow it (relax classifier) or Keep the denial?"
        )
        transcript = _write_transcript(tmp_path, [_user("get the ticket"), _assistant(step2)])

        result = handle_enforce_structured_question({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_unrelated_permissions_allow_prose_is_still_blocked(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A bare "permissions.allow" with no classifier-relax context still blocks.

        Review NB1: the bare ``permissions.allow`` marker alternative widened
        the #807 Stop-gate exemption surface — unrelated prose explaining
        allow-list syntax must NOT be exempted. It requires a relax/classifier
        token to qualify as a Step-2 classifier-denial explanation.
        """
        step = (
            "The settings schema has a permissions.allow array of glob rules. "
            "Should I document that in the README, or add an example config instead?"
        )
        transcript = _write_transcript(tmp_path, [_user("explain settings"), _assistant(step)])

        result = handle_enforce_structured_question({"transcript_path": str(transcript)})

        assert _decision(capsys).get("decision") == "block"
        assert result is True

    def test_step2_with_denied_by_classifier_signal_is_not_blocked(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Contains "denied by the classifier" — the narrowed, protocol-specific
        # phrasing (Finding 6): bare "was denied" no longer exempts; the
        # denial-source phrasing the protocol actually uses does.
        step2 = (
            "The command was denied by the classifier. "
            "Here is the smallest rule that covers it: `Edit(~/.config/*)`. "
            "Should I proceed with adding it or find another path?"
        )
        transcript = _write_transcript(tmp_path, [_user("edit config"), _assistant(step2)])

        result = handle_enforce_structured_question({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_ordinary_decision_question_still_blocked(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # No classifier-denial markers — the gate must still fire normally.
        transcript = _write_transcript(
            tmp_path,
            [_user("implement the feature"), _assistant("Should I push the branch now?")],
        )

        result = handle_enforce_structured_question({"transcript_path": str(transcript)})

        assert _decision(capsys).get("decision") == "block"
        assert result is True

    def test_classifier_relax_with_tool_call_still_passes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Step 3: the tool call IS present — already compliant, no exemption needed.
        step3 = "The command was denied. Allow it (relax classifier) or keep the denial?"
        transcript = _write_transcript(
            tmp_path,
            [_user("run it"), _assistant(step3, tool_uses=["AskUserQuestion"])],
        )

        result = handle_enforce_structured_question({"transcript_path": str(transcript)})

        assert _decision(capsys) == {}
        assert result is not True

    def test_unrelated_was_denied_prose_is_still_blocked(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A bare "was denied" with no classifier-protocol vocabulary still blocks.

        Review Finding 6: a generic ``was denied`` substring (e.g. "access was
        denied", "the MR was denied") must NOT exempt unrelated Stop-gate
        prose, otherwise the #807 structured-question gate is broadly weakened.
        """
        step = (
            "The merge request was denied review approval by the bot. "
            "Should I rebase onto main and re-request, or wait for the human reviewer?"
        )
        transcript = _write_transcript(tmp_path, [_user("ship it"), _assistant(step)])

        result = handle_enforce_structured_question({"transcript_path": str(transcript)})

        assert _decision(capsys).get("decision") == "block"
        assert result is True


class TestGateIsLoopDrivenContextAware:
    """The inline-question gate enforces only on an autonomous/loop-driven turn.

    Its whole rationale is that an inline question is invisible in an
    autonomous/loop run (it reads as a log line). In an attended interactive
    session a human IS reading the prose, so the gate is pointless nagging. It
    keys off ``_session_drives_loop`` (this session owns the tick, OR no live
    owner) and skips only when a *different* live session owns the loop. The
    over-block (must-not-fire) and under-block (must-fire) dimensions are
    asserted symmetrically; the degradation contract (unknown ⇒ fail-safe fire)
    is covered by the no-session/no-owner must-fire cases.
    """

    @staticmethod
    def _owner_record(session_id: str, pid: int) -> dict[str, dict]:
        return {
            router._OWNER_LOOP: {
                "session_id": session_id,
                "agent_id": "a",
                "pid": pid,
                "heartbeat_ts": 0,
            }
        }

    @pytest.fixture(autouse=True)
    def _registry_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        reg = tmp_path / "data"
        reg.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(reg))

    def _inline_question_transcript(self, tmp_path: Path) -> Path:
        return _write_transcript(
            tmp_path,
            [_user("implement the feature"), _assistant("Done. Should I push the branch now?")],
        )

    def test_must_fire_for_owning_loop_session(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # The owning (loop-driven) session: an inline question is invisible to
        # the loop, so the gate MUST block it.
        router._write_loop_registry(self._owner_record("s", os.getpid()))
        transcript = self._inline_question_transcript(tmp_path)

        result = handle_enforce_structured_question({"transcript_path": str(transcript), "session_id": "s"})

        assert _decision(capsys).get("decision") == "block"
        assert result is True

    def test_must_fire_for_no_owner_session_fail_safe(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # No live owner anywhere: we cannot prove the turn is attended, so the
        # gate FAILS SAFE and still fires (degradation contract).
        transcript = self._inline_question_transcript(tmp_path)

        result = handle_enforce_structured_question({"transcript_path": str(transcript), "session_id": "lonely"})

        assert _decision(capsys).get("decision") == "block"
        assert result is True

    def test_must_not_fire_for_non_owner_attended_session(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A DIFFERENT live session owns the loop; this is the attended,
        # non-owner interactive session a human is reading — no block.
        router._write_loop_registry(self._owner_record("owner-1", os.getpid()))
        transcript = self._inline_question_transcript(tmp_path)

        result = handle_enforce_structured_question({"transcript_path": str(transcript), "session_id": "attended"})

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


def _ask(questions: list[dict]) -> dict:
    return {"tool_name": "AskUserQuestion", "tool_input": {"questions": questions}}


def _q(text: str) -> dict:
    return {"question": text, "header": "h", "options": [{"label": "a", "description": "x"}]}


class TestWarnBatchedQuestions:
    """PreToolUse warn (never block) when an AskUserQuestion batches >1 question.

    The prose rule (skills/rules/SKILL.md "One decision per question") was not
    enforced for batching — the #807 gate only forces a question through the
    TOOL, not one-at-a-time. This advisory warn flags a multi-question call on
    every session (the user's "ask me the questions one by one" directive)
    while NEVER blocking the call (the user chose warn-don't-block).
    """

    def test_batched_questions_warn_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_warn_batched_questions(_ask([_q("Target branch?"), _q("Squash?")]))
        assert result is not True  # never blocks
        err = capsys.readouterr().err.lower()
        assert "one decision" in err
        assert "question" in err

    def test_single_question_is_silent(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_warn_batched_questions(_ask([_q("Push now?")]))
        assert result is not True
        assert capsys.readouterr().err == ""

    def test_non_question_tool_is_silent(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_warn_batched_questions({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        assert result is not True
        assert capsys.readouterr().err == ""

    def test_missing_questions_key_is_silent(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Malformed input must never crash or warn (crash-proof router).
        result = handle_warn_batched_questions({"tool_name": "AskUserQuestion", "tool_input": {}})
        assert result is not True
        assert capsys.readouterr().err == ""

    def test_registered_in_pretooluse_chain(self) -> None:
        assert handle_warn_batched_questions in router._HANDLERS["PreToolUse"]

    def test_runs_before_slack_mirror(self) -> None:
        # Warn before the mirror handler may deny/short-circuit a loop-driven
        # question, so the one-at-a-time advisory is emitted regardless.
        chain = router._HANDLERS["PreToolUse"]
        assert chain.index(handle_warn_batched_questions) < chain.index(router.handle_mirror_question_to_slack)
