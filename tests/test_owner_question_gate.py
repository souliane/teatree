# test-path: cross-cutting — exercises the public hook router for attended owner decisions.
"""Attended owner decisions use one plain question tool call per decision."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.owner_question_gate as owner_gate
from tests._gate_escape_audit import audited_escape

_ROUTER = Path(__file__).resolve().parents[1] / "hooks/scripts/hook_router.py"
_ADAPTER = _ROUTER.with_name("codex_hook_adapter.py")
_DISPATCHED = {"T3_DISPATCHED_AGENT": "1"}


def _transcript(tmp_path: Path, *, final: str = "") -> Path:
    path = tmp_path / "turn.jsonl"
    entries = [
        {
            "type": "user",
            "timestamp": datetime.now(UTC).isoformat(),
            "message": {"role": "user", "content": [{"type": "text", "text": "Please handle this."}]},
        }
    ]
    if final:
        entries.append(
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": final}]}}
        )
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
    return path


def _scheduled_transcript(tmp_path: Path) -> Path:
    transcript = tmp_path / "scheduled.jsonl"
    entry = {
        "type": "user",
        "turnOrigin": "scheduled",
        "timestamp": datetime.now(UTC).isoformat(),
        "message": {"role": "user", "content": [{"type": "text", "text": "run the loop"}]},
    }
    transcript.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return transcript


def _seed_settings(tmp_path: Path, settings: dict[str, object]) -> Path:
    db = tmp_path / "config.sqlite3"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)",
            [(key, json.dumps(value)) for key, value in settings.items()],
        )
    return db


def _run_hook(
    tmp_path: Path, event: str, payload: dict, *, adapter: bool = False, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    ambient = {key: value for key, value in os.environ.items() if key not in {"T3_DISPATCHED_AGENT", "T3_HOOK_CLIENT"}}
    hermetic = {
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "T3_HOOK_STATE_DIR": str(tmp_path / "hooks"),
        "TEATREE_CLAUDE_STATUSLINE_STATE_DIR": str(tmp_path / "hooks"),
        "T3_LOOP_REGISTRY_DIR": str(tmp_path / "loops"),
        "T3_CONFIG_DB": str(_seed_settings(tmp_path, {})),
    }
    return subprocess.run(
        [sys.executable, str(_ADAPTER if adapter else _ROUTER), "--event", event],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
        env=ambient | hermetic | (env or {}),
        cwd=_ROUTER.parents[2],
    )


def _ask(question: str, *, options: list[dict] | None = None) -> dict:
    return {
        "header": "Merge",
        "question": question,
        "options": options
        if options is not None
        else [
            {"label": "Merge", "description": "I merge it now."},
            {"label": "Wait", "description": "I wait for review."},
        ],
    }


def test_attended_final_decision_list_blocks(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    result = _run_hook(
        tmp_path,
        "Stop",
        {
            "session_id": "owner-turn",
            "transcript_path": str(transcript),
            "last_assistant_message": "Needs you:\n- Which branch should I use?\n- Should I merge after review?",
        },
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["decision"] == "block"
    assert "question tool" in json.loads(result.stdout)["reason"].lower()


def test_two_separate_questions_and_clean_reply_pass(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    payload = {"session_id": "owner-turn", "transcript_path": str(transcript)}
    first = _run_hook(
        tmp_path,
        "PreToolUse",
        payload | {"tool_name": "AskUserQuestion", "tool_input": {"questions": [_ask("Merge this now?")]}},
    )
    second = _run_hook(
        tmp_path,
        "PreToolUse",
        payload | {"tool_name": "AskUserQuestion", "tool_input": {"questions": [_ask("Use the main branch?")]}},
    )
    stop = _run_hook(tmp_path, "Stop", payload | {"last_assistant_message": "I have your answers and will proceed."})

    assert (first.returncode, second.returncode, stop.returncode) == (0, 0, 0)
    assert not first.stdout
    assert not second.stdout
    assert not stop.stdout


def test_normal_status_reply_passes(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    result = _run_hook(
        tmp_path,
        "Stop",
        {
            "session_id": "owner-turn",
            "transcript_path": str(transcript),
            "last_assistant_message": (
                "Since 09:12 · all overlays\nNeeds you\n"
                "  - Q12: Merge the release branch now? — answer in its Slack thread"
            ),
        },
    )

    assert result.returncode == 0
    assert not result.stdout


@pytest.mark.parametrize("question", ["Merge ticket 42 now?", "Set widget_mode now?"])
def test_question_shorthand_is_refused(tmp_path: Path, question: str) -> None:
    transcript = _transcript(tmp_path)
    result = _run_hook(
        tmp_path,
        "PreToolUse",
        {
            "session_id": "owner-turn",
            "transcript_path": str(transcript),
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [_ask(question)]},
        },
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "plain words" in result.stdout


def test_two_questions_in_one_call_are_refused(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    result = _run_hook(
        tmp_path,
        "PreToolUse",
        {
            "session_id": "owner-turn",
            "transcript_path": str(transcript),
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [_ask("Merge this now?"), _ask("Use the main branch?")]},
        },
    )

    assert result.returncode == 2
    assert "one decision per call" in result.stdout


def test_long_question_is_refused(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    question = (
        "Would you like me to merge this after I have read the review notes and checked the build result "
        "and told everyone what happened and collected the remaining comments today?"
    )
    result = _run_hook(
        tmp_path,
        "PreToolUse",
        {
            "session_id": "owner-turn",
            "transcript_path": str(transcript),
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [_ask(question)]},
        },
    )

    assert len(question) > 160
    assert result.returncode == 2
    assert "160" in result.stdout


def test_stop_uses_transcript_when_final_message_is_missing(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path, final="Needs input:\n- Which branch?\n- Merge after review?")
    result = _run_hook(tmp_path, "Stop", {"session_id": "owner-turn", "transcript_path": str(transcript)})

    assert result.returncode == 0
    assert json.loads(result.stdout)["decision"] == "block"


def test_whitespace_final_message_uses_transcript(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path, final=_DECISION_LIST)
    result = _run_hook(
        tmp_path,
        "Stop",
        {"session_id": "owner-turn", "transcript_path": str(transcript), "last_assistant_message": " \n "},
    )

    assert json.loads(result.stdout)["decision"] == "block"


def test_dispatched_final_reply_is_not_blocked(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    result = _run_hook(
        tmp_path,
        "Stop",
        {
            "session_id": "agent-turn",
            "transcript_path": str(transcript),
            "last_assistant_message": "Needs you:\n- Which branch?\n- Merge after review?",
        },
        env=_DISPATCHED,
    )

    assert result.returncode == 0
    assert not result.stdout


def test_codex_question_tool_uses_same_plain_words_check(tmp_path: Path) -> None:
    result = _run_hook(
        tmp_path,
        "PreToolUse",
        {
            "session_id": "owner-turn",
            "tool_name": "request_user_input_async",
            "tool_input": {"questions": [{"title": "Merge ticket 42 now?", "options": ["Merge", "Wait"]}]},
        },
        adapter=True,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "ticket 42" in result.stdout

    plain_result = _run_hook(
        tmp_path,
        "PreToolUse",
        {
            "session_id": "owner-turn",
            "tool_name": "request_user_input_async",
            "tool_input": {"questions": [{"title": "Merge this now?", "options": ["Merge", "Wait"]}]},
        },
        adapter=True,
    )

    assert plain_result.returncode == 0
    assert not plain_result.stdout


def test_stop_refire_does_not_block_again(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    result = _run_hook(
        tmp_path,
        "Stop",
        {
            "session_id": "owner-turn",
            "transcript_path": str(transcript),
            "stop_hook_active": True,
            "last_assistant_message": "Needs you:\n- Which branch?\n- Merge after review?",
        },
    )

    assert result.returncode == 0
    assert not result.stdout


def test_third_identical_question_refusal_fails_open(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    payload = {
        "session_id": "owner-turn",
        "transcript_path": str(transcript),
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [_ask("Merge ticket 42 now?")]},
    }

    results = [_run_hook(tmp_path, "PreToolUse", payload) for _ in range(3)]

    assert [result.returncode for result in results] == [2, 2, 0]


def test_last_assistant_message_wins_over_lagging_transcript(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path, final="Needs you:\n- Which branch?\n- Merge after review?")
    result = _run_hook(
        tmp_path,
        "Stop",
        {
            "session_id": "owner-turn",
            "transcript_path": str(transcript),
            "last_assistant_message": "I have your answer and will proceed.",
        },
    )

    assert result.returncode == 0
    assert not result.stdout


def test_malformed_question_payload_passes_without_a_content_verdict(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    result = _run_hook(
        tmp_path,
        "PreToolUse",
        {
            "session_id": "owner-turn",
            "transcript_path": str(transcript),
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{}]},
        },
    )

    assert result.returncode == 0
    assert not result.stdout


def test_codex_final_reply_uses_same_decision_prose_check(tmp_path: Path) -> None:
    result = _run_hook(
        tmp_path,
        "Stop",
        {
            "session_id": "owner-turn",
            "last_assistant_message": "Needs you:\n- Which branch?\n- Merge after review?",
        },
        adapter=True,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["decision"] == "block"


def test_loop_driven_question_skips_attended_content_gate(tmp_path: Path) -> None:
    transcript = _scheduled_transcript(tmp_path)
    result = _run_hook(
        tmp_path,
        "PreToolUse",
        {
            "session_id": "loop-turn",
            "transcript_path": str(transcript),
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [_ask("Merge ticket 42 now?")]},
        },
    )

    assert "owner-question-fit" not in result.stdout
    assert "TEATREE OWNER QUESTION GATE" not in result.stdout


def test_dispatched_question_skips_attended_content_gate(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    result = _run_hook(
        tmp_path,
        "PreToolUse",
        {
            "session_id": "agent-turn",
            "transcript_path": str(transcript),
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [_ask("Merge ticket 42 now?")]},
        },
        env=_DISPATCHED,
    )

    assert result.returncode == 0
    assert not result.stdout


_DECISION_LIST = "Needs you:\n- Which branch should I use?\n- Should I merge after review?"


def test_loop_driven_final_reply_is_not_bounced_to_the_question_tool(tmp_path: Path) -> None:
    transcript = _scheduled_transcript(tmp_path)
    result = _run_hook(
        tmp_path,
        "Stop",
        {"session_id": "loop-turn", "transcript_path": str(transcript), "last_assistant_message": _DECISION_LIST},
    )

    assert result.returncode == 0
    assert "OWNER DECISION GATE" not in result.stdout


def test_text_list_the_owner_asked_for_after_rejecting_a_question_passes(tmp_path: Path) -> None:
    now = datetime.now(UTC).isoformat()
    ask = {"type": "tool_use", "id": "toolu_1", "name": "AskUserQuestion", "input": {"questions": [_ask("Merge?")]}}
    entries = [
        {"type": "assistant", "message": {"role": "assistant", "content": [ask]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1"}]}},
        {
            "type": "user",
            "timestamp": now,
            "message": {"role": "user", "content": [{"type": "text", "text": "None of these, list them as text."}]},
        },
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": _DECISION_LIST}]}},
    ]
    transcript = tmp_path / "rejected.jsonl"
    transcript.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")

    result = _run_hook(
        tmp_path,
        "Stop",
        {"session_id": "owner-turn", "transcript_path": str(transcript), "last_assistant_message": _DECISION_LIST},
    )

    assert result.returncode == 0
    assert not result.stdout


def test_classifier_denial_explanation_passes(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    text = "The write was denied by the classifier.\n" + _DECISION_LIST
    result = _run_hook(
        tmp_path,
        "Stop",
        {"session_id": "owner-turn", "transcript_path": str(transcript), "last_assistant_message": text},
    )

    assert result.returncode == 0
    assert not result.stdout


def test_legacy_token_does_not_let_the_final_reply_end(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    text = _DECISION_LIST + "\n[owner-question-ok: the owner asked for this list in text]"
    result = _run_hook(
        tmp_path,
        "Stop",
        {"session_id": "owner-turn", "transcript_path": str(transcript), "last_assistant_message": text},
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["decision"] == "block"


def test_empty_escape_reason_still_blocks(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    result = _run_hook(
        tmp_path,
        "Stop",
        {
            "session_id": "owner-turn",
            "transcript_path": str(transcript),
            "last_assistant_message": _DECISION_LIST + "\n[owner-question-ok: ]",
        },
    )

    assert json.loads(result.stdout)["decision"] == "block"


def test_kill_switch_lets_both_arms_through(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    payload = {"session_id": "owner-turn", "transcript_path": str(transcript)}
    _seed_settings(tmp_path, {"owner_question_gate_enabled": False})

    stop = _run_hook(tmp_path, "Stop", payload | {"last_assistant_message": _DECISION_LIST})
    ask = _run_hook(
        tmp_path,
        "PreToolUse",
        payload | {"tool_name": "AskUserQuestion", "tool_input": {"questions": [_ask("Merge ticket 42 now?")]}},
    )

    assert (stop.returncode, stop.stdout) == (0, "")
    assert ask.returncode == 0
    assert "OWNER QUESTION GATE" not in ask.stdout
    assert ask.stderr == ""


def test_legacy_token_does_not_let_a_question_through(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    question = "Merge ticket 42 now? [owner-question-ok: the owner asked for the number]"
    result = _run_hook(
        tmp_path,
        "PreToolUse",
        {
            "session_id": "owner-turn",
            "transcript_path": str(transcript),
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [_ask(question)]},
        },
    )

    assert result.returncode == 2
    assert "OWNER QUESTION GATE" in result.stdout


@pytest.mark.parametrize("dispatched", [False, True])
def test_batched_question_the_gate_does_not_refuse_still_warns(tmp_path: Path, *, dispatched: bool) -> None:
    transcript = _transcript(tmp_path) if dispatched else _scheduled_transcript(tmp_path)
    result = _run_hook(
        tmp_path,
        "PreToolUse",
        {
            "session_id": "loop-turn",
            "transcript_path": str(transcript),
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [_ask("Merge this now?"), _ask("Use the main branch?")]},
        },
        env=_DISPATCHED if dispatched else None,
    )

    assert "OWNER QUESTION GATE" not in result.stdout
    assert "ONE decision per call" in result.stderr


@pytest.mark.parametrize(
    "text",
    [
        "Needs you: (none)",
        "Needs you: —",
        "Needs you: no",
        "Needs you: all clear",
        "Waiting on you: 0",
        "Needs you:",
        "Waiting on you: zero",
    ],
)
def test_empty_owner_labels_pass_through_router(tmp_path: Path, text: str) -> None:
    transcript = _transcript(tmp_path)
    result = _run_hook(
        tmp_path,
        "Stop",
        {"session_id": "owner-turn", "transcript_path": str(transcript), "last_assistant_message": text},
    )

    assert result.returncode == 0
    assert not result.stdout


@pytest.mark.parametrize(
    "text",
    [
        "Review done. Pull requests that are ok to merge:\n- First\n- Second",
        "If you want me to look later, here are the logs:\n- First\n- Second",
    ],
)
def test_status_intro_with_two_bullets_passes_router(tmp_path: Path, text: str) -> None:
    transcript = _transcript(tmp_path)
    result = _run_hook(
        tmp_path,
        "Stop",
        {"session_id": "owner-turn", "transcript_path": str(transcript), "last_assistant_message": text},
    )

    assert result.returncode == 0
    assert not result.stdout


@pytest.mark.parametrize(
    "text", ["Your call — I went with the smaller change and pushed it.", "Approve? Answered yes."]
)
def test_label_recaps_pass_router(tmp_path: Path, text: str) -> None:
    transcript = _transcript(tmp_path)
    result = _run_hook(
        tmp_path,
        "Stop",
        {"session_id": "owner-turn", "transcript_path": str(transcript), "last_assistant_message": text},
    )

    assert result.returncode == 0
    assert not result.stdout


@pytest.mark.parametrize(
    "text",
    [
        "Needs you: none; choose the branch.",
        "Needs you: none; please choose the branch.",
        "Needs you \u2013 pick one:\n- Merge now\n- Wait for review",
    ],
)
def test_owner_label_with_real_decision_blocks_router(tmp_path: Path, text: str) -> None:
    transcript = _transcript(tmp_path)
    result = _run_hook(
        tmp_path,
        "Stop",
        {"session_id": "owner-turn", "transcript_path": str(transcript), "last_assistant_message": text},
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["decision"] == "block"


def test_plain_client_question_words_pass(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    result = _run_hook(
        tmp_path,
        "PreToolUse",
        {
            "session_id": "owner-turn",
            "transcript_path": str(transcript),
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [_ask("Should the deferred migration run now?")]},
        },
    )

    assert result.returncode == 0
    assert not result.stdout


@pytest.mark.parametrize("bad", [{}, None])
def test_malformed_item_does_not_hide_batch_refusal(tmp_path: Path, bad: object) -> None:
    transcript = _transcript(tmp_path)
    result = _run_hook(
        tmp_path,
        "PreToolUse",
        {
            "session_id": "owner-turn",
            "transcript_path": str(transcript),
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [_ask("Merge this now?"), bad]},
        },
    )

    assert result.returncode == 2
    assert "one decision per call" in result.stdout


def test_malformed_item_does_not_hide_other_item_shorthand(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    result = _run_hook(
        tmp_path,
        "PreToolUse",
        {
            "session_id": "owner-turn",
            "transcript_path": str(transcript),
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{}, _ask("Merge ticket 42 now?")]},
        },
    )

    assert result.returncode == 2
    assert "internal shorthand" in result.stdout


def test_whitespace_dispatched_marker_does_not_skip_gate(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    result = _run_hook(
        tmp_path,
        "PreToolUse",
        {
            "session_id": "owner-turn",
            "transcript_path": str(transcript),
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [_ask("Merge ticket 42 now?")]},
        },
        env={"T3_DISPATCHED_AGENT": "  "},
    )

    assert result.returncode == 2


def test_router_refuses_a_123_word_card(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path)
    card = _ask(
        "Merge this now?",
        options=[
            {"label": "Merge", "description": "a " * 58},
            {"label": "Wait", "description": "b " * 59},
        ],
    )
    result = _run_hook(
        tmp_path,
        "PreToolUse",
        {
            "session_id": "owner-turn",
            "transcript_path": str(transcript),
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [card]},
        },
    )

    assert result.returncode == 2
    assert "123 words; at most 120" in result.stdout


def test_skipped_owner_wording_gate_names_no_remote_ci_backstop(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "teatree.core.modelkit.question_card", None)
    monkeypatch.setattr(owner_gate, "_dispatched_agent", lambda: False)
    monkeypatch.setattr(owner_gate, "_gate_enabled", lambda: True)
    monkeypatch.setattr(owner_gate, "_owner_reads_this_turn", lambda _data: True)

    owner_gate.handle_block_unfit_question(
        {"tool_name": "AskUserQuestion", "tool_input": {"questions": [_ask("Merge this now?")]}}
    )

    warning = capsys.readouterr().err
    assert "owner-question-fit" in warning
    assert "remote CI job remains the backstop" not in warning


def test_two_questions_still_refused_when_card_import_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "teatree.core.modelkit.question_card", None)
    monkeypatch.setattr(owner_gate, "_dispatched_agent", lambda: False)
    monkeypatch.setattr(owner_gate, "_gate_enabled", lambda: True)
    monkeypatch.setattr(owner_gate, "_owner_reads_this_turn", lambda _data: True)
    data = {
        "session_id": "owner-turn",
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [_ask("Merge now?"), _ask("Wait for review?")]},
    }

    assert owner_gate.handle_block_unfit_question(data)
    assert "one decision per call" in capsys.readouterr().out


def test_disabled_question_gate_skips_card_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("T3_DISPATCHED_AGENT", raising=False)
    monkeypatch.setattr(owner_gate, "_gate_enabled", lambda: False)
    monkeypatch.setattr(owner_gate, "_fit_problems", lambda _questions: pytest.fail("validated card"))

    assert not owner_gate.handle_block_unfit_question(
        {"tool_name": "AskUserQuestion", "tool_input": {"questions": [_ask("Merge this now?")]}}
    )


def test_disabled_stop_gate_does_not_resolve_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("T3_DISPATCHED_AGENT", raising=False)
    monkeypatch.setattr(owner_gate, "_gate_enabled", lambda: False)
    monkeypatch.setattr(owner_gate, "current_turn_final_text", lambda _path: pytest.fail("parsed transcript"))
    monkeypatch.setattr(owner_gate, "find_decision_prose", lambda _text: pytest.fail("ran detector"))

    assert owner_gate.handle_block_decision_prose({"transcript_path": "missing"}) is None


def test_pretooluse_attended_check_is_read_once_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("T3_DISPATCHED_AGENT", raising=False)
    reads = 0

    def live(_data: dict) -> bool:
        nonlocal reads
        reads += 1
        return True

    monkeypatch.setattr(router, "_is_live_user_turn", live)
    monkeypatch.setattr(owner_gate, "_fit_problems", lambda _questions: ["problem"])
    monkeypatch.setattr(router, "_fail_open_or_deny", lambda *_args, **_kwargs: False)
    data = {"tool_name": "AskUserQuestion", "tool_input": {"questions": [_ask("Merge this now?")]}}

    owner_gate.handle_block_unfit_question(data)
    router.handle_mirror_question_to_slack(data)

    assert reads == 1


@pytest.mark.parametrize("recorded", [False, True])
def test_question_escape_requires_an_audit(*, recorded: bool, capsys: pytest.CaptureFixture[str]) -> None:
    data = {
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [_ask("Merge ticket 42 now? [gate-escape:owner-question-fit: fixture]")]},
    }
    with (
        patch.object(owner_gate, "_dispatched_agent", return_value=False),
        patch.object(owner_gate, "_gate_enabled", return_value=True),
        patch.object(owner_gate, "_owner_reads_this_turn", return_value=True),
        patch.object(owner_gate, "_fit_problems", return_value=["question too specific"]),
        audited_escape(data, recorded=recorded) as requests,
    ):
        blocked = owner_gate.handle_block_unfit_question(data)

    assert blocked is not recorded
    assert [request["gate"] for request in requests] == ["owner-question-fit"]
    if recorded:
        assert "recorded for this action" in capsys.readouterr().err


def test_legacy_owner_question_marker_does_not_bypass_question() -> None:
    data = {
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [_ask("Merge ticket 42 now? [owner-question-ok: old]")]},
    }
    with (
        patch.object(owner_gate, "_dispatched_agent", return_value=False),
        patch.object(owner_gate, "_gate_enabled", return_value=True),
        patch.object(owner_gate, "_owner_reads_this_turn", return_value=True),
        patch.object(owner_gate, "_fit_problems", return_value=["question too specific"]),
        audited_escape(data) as requests,
    ):
        assert owner_gate.handle_block_unfit_question(data)

    assert requests == []


@pytest.mark.parametrize("recorded", [False, True])
def test_final_reply_escape_requires_an_audit(*, recorded: bool, capsys: pytest.CaptureFixture[str]) -> None:
    data = {
        "last_assistant_message": "Needs you: merge this now?\n[gate-escape:owner-question-decision: fixture]",
    }
    with (
        patch.object(owner_gate, "_dispatched_agent", return_value=False),
        patch.object(owner_gate, "_gate_enabled", return_value=True),
        patch.object(owner_gate, "_owner_reads_this_turn", return_value=True),
        patch.object(owner_gate, "_sanctioned_prose", return_value=False),
        patch.object(owner_gate, "find_decision_prose", return_value="merge this now?"),
        audited_escape(data, recorded=recorded, event="Stop") as requests,
    ):
        blocked = owner_gate.handle_block_decision_prose(data)

    assert (blocked is True) is not recorded
    assert [request["gate"] for request in requests] == ["owner-question-decision"]
    if recorded:
        assert "recorded for this action" in capsys.readouterr().err


def test_legacy_owner_question_marker_does_not_bypass_final_reply() -> None:
    data = {"last_assistant_message": "Needs you: merge this now?\n[owner-question-ok: old]"}
    with (
        patch.object(owner_gate, "_dispatched_agent", return_value=False),
        patch.object(owner_gate, "_gate_enabled", return_value=True),
        patch.object(owner_gate, "_owner_reads_this_turn", return_value=True),
        patch.object(owner_gate, "_sanctioned_prose", return_value=False),
        patch.object(owner_gate, "find_decision_prose", return_value="merge this now?"),
        audited_escape(data, event="Stop") as requests,
    ):
        assert owner_gate.handle_block_decision_prose(data) is True

    assert requests == []
