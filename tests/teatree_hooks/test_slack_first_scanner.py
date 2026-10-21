# test-path: cross-cutting
# Drives hooks/scripts/slack_first_gate.py (no src/teatree mirror) and its detector, teatree.hooks.slack_first_scanner.
"""Slack-first Stop gate: while factory Slack is live an owner prompt is relayed, not answered in the terminal.

The real Stop handler, a real transcript JSONL under ``tmp_path`` and the published verdict file the
worker writes — only stdout and the environment cross the boundary.
"""

import json
import time
from pathlib import Path
from typing import Final
from unittest.mock import patch

import pytest

from hooks.scripts.hook_router import _HANDLERS, handle_answer_first_gate
from hooks.scripts.loop_prompt_shape import LOOP_PROMPT
from hooks.scripts.slack_first_gate import handle_slack_first_gate
from teatree import standing_directives_cache
from teatree.hooks.slack_first_scanner import RELAY_RECEIPT_MARKER, RELAY_REFUSED_MARKER
from tests._skill_load_transcript import skill_load_entries

_SESSION: Final = "sess-1"
_PROMPT: Final = "can you check why the 4001 pipeline is red?"
_ANSWER: Final = "The pipeline is red because the shard timed out."
_RECEIPT: Final = f"{RELAY_RECEIPT_MARKER} pending_chat=7 ts=1700000000.000001 permalink=https://slack.test/p1"
_STANDING_RULES: Final = (
    "<system-reminder>Standing rules for this session (3) — they apply to your next reply:\n"
    "  - [standing-golden-rule] Plan, implement, review.</system-reminder>"
)
_NOTICE: Final = "<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n</task-notification>"


def _user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _assistant(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _tool_call(tool_use_id: str = "toolu_01") -> dict:
    block = {"type": "tool_use", "id": tool_use_id, "name": "Bash", "input": {}}
    return {"type": "assistant", "message": {"role": "assistant", "content": [block]}}


def _tool_result(output: str, tool_use_id: str = "toolu_01") -> dict:
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": output}
    return {"type": "user", "message": {"role": "user", "content": [block]}}


def _transcript(tmp_path: Path, *entries: dict) -> str:
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
    return str(path)


def _publish(*, live: bool, age_seconds: float = 0.0) -> None:
    standing_directives_cache.write_factory_slack(
        {
            "overlay": "t3-slack",
            "live": live,
            "reason": "" if live else "heartbeat_absent",
            "checked_at": time.time() - age_seconds,
            "open_relays": {},
        }
    )


@pytest.fixture(autouse=True)
def attended_engaged_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("T3_HOOK_STATE_DIR", str(state))
    monkeypatch.setenv("TEATREE_CLAUDE_STATUSLINE_STATE_DIR", str(state))
    monkeypatch.setenv("T3_AUTOLOAD", "0")
    for name in ("CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ATTENDED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CLAUDE_CODE_CHILD_SESSION", raising=False)
    (state / f"{_SESSION}.teatree-active").touch()


def _stop(transcript: str, **extra: object) -> dict:
    return {"session_id": _SESSION, "transcript_path": transcript, **extra}


def _decision(capsys: pytest.CaptureFixture[str]) -> dict:
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else {}


def test_any_genuine_owner_prompt_without_relay_receipt_blocks_when_slack_live(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _publish(live=True)
    transcript = _transcript(tmp_path, _user(_PROMPT), _assistant(_ANSWER))

    result = handle_slack_first_gate(_stop(transcript))

    decision = _decision(capsys)
    assert result is True
    assert decision["decision"] == "block"
    assert "SLACK-FIRST GATE" in decision["reason"]
    assert _PROMPT in decision["reason"]
    assert "pending_chat relay" in decision["reason"]
    assert "[skip-slack-first: <reason>]" in decision["reason"]


def test_terminal_answer_allowed_when_slack_not_live(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    transcript = _transcript(tmp_path, _user(_PROMPT), _assistant(_ANSWER))

    for name, publish in {
        "unpublished": lambda: None,
        "not live": lambda: _publish(live=False),
        "stale": lambda: _publish(live=True, age_seconds=standing_directives_cache.FACTORY_SLACK_FRESH_SECONDS + 60),
    }.items():
        publish()

        assert handle_slack_first_gate(_stop(transcript)) is None, name
        assert capsys.readouterr().out == "", name


def test_standing_directive_and_tick_context_never_trigger_a_relay(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _publish(live=True)
    only_ambient = {
        "standing directive": [_user(_STANDING_RULES), _assistant(_ANSWER)],
        "loop tick": [_user(LOOP_PROMPT), _assistant("tick done")],
        "tick with standing rules": [_user(f"{_STANDING_RULES}\n{LOOP_PROMPT}"), _assistant("tick done")],
    }

    for name, entries in only_ambient.items():
        assert handle_slack_first_gate(_stop(_transcript(tmp_path, *entries))) is None, name

    mixed = _transcript(tmp_path, _user(f"{_STANDING_RULES}\n{_PROMPT}"), _assistant(_ANSWER))
    assert handle_slack_first_gate(_stop(mixed)) is True
    assert "SLACK-FIRST GATE" in _decision(capsys)["reason"]


class TestARelayInTheFinalTurnSatisfiesTheGate:
    @pytest.fixture(autouse=True)
    def live(self) -> None:
        _publish(live=True)

    @pytest.mark.parametrize("marker", [_RECEIPT, f"{RELAY_REFUSED_MARKER} slack_not_live: heartbeat_stale"])
    def test_a_receipt_or_a_refusal_in_a_tool_result_allows(self, tmp_path: Path, marker: str) -> None:
        transcript = _transcript(
            tmp_path, _user(_PROMPT), _tool_call(), _tool_result(json.dumps({"marker": marker})), _assistant("Relayed.")
        )

        assert handle_slack_first_gate(_stop(transcript)) is None

    @pytest.mark.parametrize(
        "source_read",
        [
            f'RELAY_RECEIPT_MARKER: Final = "{RELAY_RECEIPT_MARKER}"',
            f"the command prints `{RELAY_REFUSED_MARKER}` with exit 3 when the prompt is blank",
            f"{RELAY_RECEIPT_MARKER} is the line the relay prints",
            f"{RELAY_RECEIPT_MARKER} pending_chat=7 ts=",
        ],
        ids=["constant", "prose-refusal", "prose-receipt", "receipt-shape-without-a-ts"],
    )
    def test_reading_the_relay_source_or_docs_in_the_turn_is_not_a_relay(
        self, tmp_path: Path, source_read: str
    ) -> None:
        transcript = _transcript(
            tmp_path, _user(_PROMPT), _tool_call(), _tool_result(source_read), _assistant("The relay prints a marker.")
        )

        assert handle_slack_first_gate(_stop(transcript)) is True

    def test_a_marker_the_agent_typed_into_its_own_prose_does_not_count(self, tmp_path: Path) -> None:
        transcript = _transcript(tmp_path, _user(_PROMPT), _assistant(f"All done. {_RECEIPT}"))

        assert handle_slack_first_gate(_stop(transcript)) is True

    def test_a_receipt_from_an_earlier_prompt_does_not_cover_a_new_one(self, tmp_path: Path) -> None:
        transcript = _transcript(
            tmp_path,
            _user("first question?"),
            _tool_call(),
            _tool_result(_RECEIPT),
            _assistant("Relayed."),
            _user(_PROMPT),
            _assistant(_ANSWER),
        )

        assert handle_slack_first_gate(_stop(transcript)) is True


class TestASkillLoadedMidTurnDoesNotHideTheOwnersPrompt:
    """A skill's ``isMeta`` body, loaded before nearly every reply, must not end the walk back to the prompt."""

    @pytest.fixture(autouse=True)
    def live(self) -> None:
        _publish(live=True)

    def test_an_answer_after_a_skill_load_is_still_blocked(self, tmp_path: Path) -> None:
        transcript = _transcript(tmp_path, _user(_PROMPT), *skill_load_entries(), _assistant(_ANSWER))

        assert handle_slack_first_gate(_stop(transcript)) is True

    def test_a_receipt_printed_before_a_skill_load_still_satisfies_the_gate(self, tmp_path: Path) -> None:
        transcript = _transcript(
            tmp_path,
            _user(_PROMPT),
            _tool_call(),
            _tool_result(json.dumps({"marker": _RECEIPT})),
            *skill_load_entries(),
            _assistant("Relayed."),
        )

        assert handle_slack_first_gate(_stop(transcript)) is None


class TestABackgroundTaskNotice:
    """Claude Code writes a notice that lands mid-turn as an attachment, and one that finds it idle as a user turn."""

    @pytest.fixture(autouse=True)
    def live(self) -> None:
        _publish(live=True)

    def test_a_notice_queued_mid_turn_does_not_hide_the_owners_prompt(self, tmp_path: Path) -> None:
        queued = {
            "type": "attachment",
            "attachment": {"type": "queued_command", "commandMode": "task-notification", "prompt": _NOTICE},
        }
        transcript = _transcript(
            tmp_path, _user(_PROMPT), _tool_call(), queued, _tool_result("ok"), _assistant(_ANSWER)
        )

        assert handle_slack_first_gate(_stop(transcript)) is True

    def test_a_turn_a_notice_started_is_not_the_owners(self, tmp_path: Path) -> None:
        notice_turn = {
            "type": "user",
            "origin": {"kind": "task-notification"},
            "message": {"role": "user", "content": _NOTICE},
        }
        transcript = _transcript(tmp_path, _user(_PROMPT), _assistant("Relayed."), notice_turn, _assistant(_ANSWER))

        assert handle_slack_first_gate(_stop(transcript)) is None


class TestNeverLockout:
    @pytest.fixture(autouse=True)
    def live(self) -> None:
        _publish(live=True)

    def _blocking_transcript(self, tmp_path: Path, answer: str = _ANSWER) -> str:
        return _transcript(tmp_path, _user(_PROMPT), _assistant(answer))

    def test_the_stop_hook_active_refire_is_never_blocked(self, tmp_path: Path) -> None:
        assert handle_slack_first_gate(_stop(self._blocking_transcript(tmp_path), stop_hook_active=True)) is None

    def test_a_skip_token_with_a_reason_clears_one_stop(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        transcript = self._blocking_transcript(tmp_path, f"{_ANSWER}\n[skip-slack-first: the owner said answer here]")

        assert handle_slack_first_gate(_stop(transcript)) is None
        assert "the owner said answer here" in capsys.readouterr().err

    def test_a_skip_token_without_a_reason_does_not_count(self, tmp_path: Path) -> None:
        transcript = self._blocking_transcript(tmp_path, f"{_ANSWER}\n[skip-slack-first: ]")

        assert handle_slack_first_gate(_stop(transcript)) is True

    def test_the_factory_lane_an_unattended_session_and_an_unengaged_one_are_never_gated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transcript = self._blocking_transcript(tmp_path)
        for name, value in (
            ("CLAUDE_AGENT_SDK_VERSION", "0.1.0"),
            ("CLAUDE_CODE_ENTRYPOINT", "sdk-py"),
            ("CLAUDE_CODE_SESSION_ATTENDED", "0"),
            ("CLAUDE_CODE_CHILD_SESSION", "1"),
        ):
            with monkeypatch.context() as patched:
                patched.setenv(name, value)

                assert handle_slack_first_gate(_stop(transcript)) is None, name

        assert handle_slack_first_gate({"session_id": "sess-unengaged", "transcript_path": transcript}) is None

    def test_a_crash_inside_the_detector_allows_the_stop(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("teatree.hooks.slack_first_scanner.find_unrelayed_prompt", side_effect=RuntimeError("boom")):
            assert handle_slack_first_gate(_stop(self._blocking_transcript(tmp_path))) is None

        assert capsys.readouterr().out == ""

    def test_it_runs_in_the_stop_chain_right_after_the_answer_first_gate(self) -> None:
        chain = _HANDLERS["Stop"]

        assert chain.index(handle_slack_first_gate) == chain.index(handle_answer_first_gate) + 1
