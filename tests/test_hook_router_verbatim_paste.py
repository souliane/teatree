# test-path: cross-cutting
# Exercises the hooks/scripts/verbatim_paste_gate.py handlers wired into
# hook_router.py (no src/teatree mirror), so it spans packages.
"""The publish gate that asks whether a body reproduces the operator (#4195).

The anti-vacuous proof this file exists for: with the operator's message
recorded, NO registered PreToolUse handler refused a public issue body that
blockquoted it — the banned-terms gate cleared it, because a term list has no
token for "this is someone's private message". That case is the first class
below, run against the whole registered chain rather than this gate alone.

The rest pin the postures the refusal rests on: scoped to a public forge post,
UNKNOWN announced rather than reported clean, and every escape (override,
kill-switch, internal error) leaving the call allowed.
"""

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.verbatim_paste_gate as gate
from hooks.scripts.loop_prompt_shape import LOOP_PROMPT
from teatree.hooks import verbatim_paste as vp

_SESSION = "sess-paste-4195"
_OPERATOR_MESSAGE = (
    "Stop pasting my chat messages into public issues verbatim. I want you to "
    "write the summary in your own words every single time, without exception."
)
_PUBLIC_POST = (
    'gh issue create --repo souliane/teatree --title "Post-mortem" '
    f'--body "## Why\n\nThe request was:\n\n> {_OPERATOR_MESSAGE}\n"'
)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.delenv(vp.OVERRIDE_ENV, raising=False)


@pytest.fixture
def recorded() -> None:
    gate.handle_record_operator_message({"session_id": _SESSION, "prompt": _OPERATOR_MESSAGE})


def _event(command: str, session_id: str = _SESSION) -> dict:
    return {"session_id": session_id, "tool_name": "Bash", "tool_input": {"command": command}}


def _run(command: str, session_id: str = _SESSION) -> tuple[bool, str]:
    out, err = StringIO(), StringIO()
    with patch("sys.stdout", out), patch("sys.stderr", err):
        blocked = gate.handle_block_verbatim_operator_paste(_event(command, session_id))
    return blocked, err.getvalue()


def _chain_denies(command: str) -> bool:
    """True iff ANY registered PreToolUse handler refuses *command*."""
    out, err = StringIO(), StringIO()
    with patch("sys.stdout", out), patch("sys.stderr", err):
        return any(handler(_event(command)) for handler in router._HANDLERS["PreToolUse"])


class TestRegisteredChainRefusesTheOperatorPaste:
    def test_some_registered_handler_denies_the_measured_body(self, recorded: None) -> None:
        assert _chain_denies(_PUBLIC_POST) is True

    def test_a_paraphrased_body_passes_the_whole_chain(self, recorded: None) -> None:
        paraphrased = (
            'gh issue create --repo souliane/teatree --title "Post-mortem" '
            '--body "The operator asked for their chat text to be summarised, never reproduced."'
        )
        assert _chain_denies(paraphrased) is False


class TestTheRefusal:
    def test_the_deny_names_the_span(self, recorded: None) -> None:
        out, err = StringIO(), StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            blocked = gate.handle_block_verbatim_operator_paste(_event(_PUBLIC_POST))
        assert blocked is True
        reason = json.loads(out.getvalue().strip())["hookSpecificOutput"]["permissionDecisionReason"]
        assert "pasting my chat messages into public issues" in reason
        assert "#4195" in reason

    def test_the_deny_is_recorded(self, recorded: None, tmp_path: Path) -> None:
        _run(_PUBLIC_POST)
        audit = (tmp_path / "state" / "verbatim-paste.jsonl").read_text(encoding="utf-8")
        assert json.loads(audit.strip())["decision"] == "blocked"


class TestScope:
    def test_a_non_publish_command_is_ignored(self, recorded: None) -> None:
        assert _run(f'echo "> {_OPERATOR_MESSAGE}"')[0] is False

    def test_a_local_commit_is_not_a_publish_surface(self, recorded: None) -> None:
        assert _run(f'git commit -m "> {_OPERATOR_MESSAGE}"')[0] is False

    def test_a_non_bash_tool_is_ignored(self, recorded: None) -> None:
        payload = {"session_id": _SESSION, "tool_name": "Edit", "tool_input": {"new_string": _OPERATOR_MESSAGE}}
        assert gate.handle_block_verbatim_operator_paste(payload) is False


class TestUnknownIsAnnouncedNotSilent:
    def test_an_unrecorded_session_allows_but_says_it_could_not_check(self) -> None:
        blocked, stderr = _run(_PUBLIC_POST, session_id="never-seen")
        assert blocked is False
        assert "could NOT check" in stderr

    def test_the_unknown_outcome_is_recorded(self, tmp_path: Path) -> None:
        _run(_PUBLIC_POST, session_id="never-seen")
        audit = (tmp_path / "state" / "verbatim-paste.jsonl").read_text(encoding="utf-8")
        assert json.loads(audit.strip())["outcome"] == vp.UNKNOWN


class TestEscapes:
    def test_the_env_prefix_override_allows_and_is_recorded(self, recorded: None, tmp_path: Path) -> None:
        blocked, stderr = _run(f"ALLOW_VERBATIM_PASTE=1 {_PUBLIC_POST}")
        assert blocked is False
        assert "override" in stderr
        audit = (tmp_path / "state" / "verbatim-paste.jsonl").read_text(encoding="utf-8")
        assert json.loads(audit.strip())["decision"] == "override"

    def test_the_kill_switch_disables_the_gate(self, recorded: None) -> None:
        with patch.object(gate, "_teatree_bool_setting", return_value=False):
            assert gate.handle_block_verbatim_operator_paste(_event(_PUBLIC_POST)) is False

    def test_an_internal_error_fails_open_loudly(self, recorded: None) -> None:
        err = StringIO()
        with patch("sys.stderr", err), patch.object(gate, "_run_verbatim_paste_pretool", side_effect=RuntimeError("x")):
            blocked = gate.handle_block_verbatim_operator_paste(_event(_PUBLIC_POST))
        assert blocked is False
        assert "failed open" in err.getvalue()
        assert "NOT a clean scan" in err.getvalue()


class TestRecorder:
    def test_a_bare_loop_tick_is_not_operator_speech(self, tmp_path: Path) -> None:
        gate.handle_record_operator_message({"session_id": "sess-tick", "prompt": LOOP_PROMPT})
        assert not vp.ledger_path("sess-tick", tmp_path / "state").exists()

    def test_harness_ambient_context_is_not_recorded_as_operator_words(self, tmp_path: Path) -> None:
        ambient = f"<system-reminder>{_OPERATOR_MESSAGE}</system-reminder>\nplease continue with the ticket"
        gate.handle_record_operator_message({"session_id": "sess-ambient", "prompt": ambient})
        verdict = vp.scan_body(f"> {_OPERATOR_MESSAGE}", session_id="sess-ambient", root=tmp_path / "state")
        assert verdict.outcome == vp.CLEAN

    def test_a_prompt_with_no_session_id_records_nothing(self, tmp_path: Path) -> None:
        gate.handle_record_operator_message({"session_id": "", "prompt": _OPERATOR_MESSAGE})
        assert not (tmp_path / "state").exists()

    def test_the_recorder_never_raises(self) -> None:
        with patch.object(gate, "is_bare_loop_prompt", side_effect=RuntimeError("boom")):
            gate.handle_record_operator_message({"session_id": _SESSION, "prompt": _OPERATOR_MESSAGE})


class TestWiring:
    def test_both_handlers_are_registered(self) -> None:
        assert gate.handle_record_operator_message in router._HANDLERS["UserPromptSubmit"]
        assert gate.handle_block_verbatim_operator_paste in router._HANDLERS["PreToolUse"]

    def test_the_gate_runs_after_the_banned_terms_gate(self) -> None:
        chain = router._HANDLERS["PreToolUse"]
        assert chain.index(gate.handle_block_verbatim_operator_paste) > chain.index(router.handle_banned_terms_pretool)
