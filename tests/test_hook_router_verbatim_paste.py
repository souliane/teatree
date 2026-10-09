# test-path: cross-cutting
# Exercises the hooks/scripts/verbatim_paste_gate.py handlers wired into
# hook_router.py (no src/teatree mirror), so it spans packages.
"""The publish gate that asks whether a body reproduces the operator (#4195).

The anti-vacuous proof this file exists for: with the operator's message in
the session transcript, NO registered PreToolUse handler refused a public issue body that
blockquoted it — the banned-terms gate cleared it, because a term list has no
token for "this is someone's private message". That case is the first class
below, run against the whole registered chain rather than this gate alone.

The rest pin the postures the refusal rests on: scoped to a public forge post,
UNKNOWN announced rather than reported clean, and the kill-switch and internal
error behavior.
"""

import json
import sqlite3
from contextlib import closing
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.verbatim_paste_gate as gate
from hooks.scripts.loop_prompt_shape import LOOP_PROMPT
from teatree.hooks import _repo_visibility
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
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured_banned_term_registry: None) -> None:
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "state"))


def _transcript(tmp_path: Path, *prompts: str) -> str:
    path = tmp_path / "transcript.jsonl"
    entries = [
        {"type": "user", "origin": {"kind": "human"}, "message": {"role": "user", "content": p}} for p in prompts
    ]
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    return str(path)


@pytest.fixture
def said(tmp_path: Path) -> str:
    return _transcript(tmp_path, _OPERATOR_MESSAGE)


def _event(command: str, transcript_path: str = "") -> dict:
    return {
        "session_id": _SESSION,
        "transcript_path": transcript_path,
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


def _run(command: str, transcript_path: str = "") -> tuple[bool, str]:
    out, err = StringIO(), StringIO()
    with patch("sys.stdout", out), patch("sys.stderr", err):
        blocked = gate.handle_block_verbatim_operator_paste(_event(command, transcript_path))
    return blocked, err.getvalue()


def _chain_denies(command: str, transcript_path: str) -> bool:
    """True iff ANY registered PreToolUse handler refuses *command*."""
    out, err = StringIO(), StringIO()
    with patch("sys.stdout", out), patch("sys.stderr", err):
        return any(handler(_event(command, transcript_path)) for handler in router._HANDLERS["PreToolUse"])


class TestRegisteredChainRefusesTheOperatorPaste:
    def test_some_registered_handler_denies_the_measured_body(self, said: str) -> None:
        assert _chain_denies(_PUBLIC_POST, said) is True

    def test_a_paraphrased_body_passes_the_whole_chain(self, said: str) -> None:
        paraphrased = (
            'gh issue create --repo souliane/teatree --title "Post-mortem" '
            '--body "The operator asked for their chat text to be summarised, never reproduced."'
        )
        assert _chain_denies(paraphrased, said) is False


class TestTheRefusal:
    def test_the_deny_names_the_span(self, said: str) -> None:
        out, err = StringIO(), StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            blocked = gate.handle_block_verbatim_operator_paste(_event(_PUBLIC_POST, said))
        assert blocked is True
        reason = json.loads(out.getvalue().strip())["hookSpecificOutput"]["permissionDecisionReason"]
        assert "pasting my chat messages into public issues" in reason
        assert "#4195" in reason

    def test_the_deny_is_recorded(self, said: str, tmp_path: Path) -> None:
        _run(_PUBLIC_POST, said)
        audit = (tmp_path / "state" / "verbatim-paste.jsonl").read_text(encoding="utf-8")
        assert json.loads(audit.strip())["decision"] == "blocked"


class TestScope:
    def test_a_non_publish_command_is_ignored(self, said: str) -> None:
        assert _run(f'echo "> {_OPERATOR_MESSAGE}"', said)[0] is False

    def test_a_local_commit_is_not_a_publish_surface(self, said: str) -> None:
        assert _run(f'git commit -m "> {_OPERATOR_MESSAGE}"', said)[0] is False

    def test_a_non_bash_tool_is_ignored(self, said: str) -> None:
        payload = {"session_id": _SESSION, "tool_name": "Edit", "tool_input": {"new_string": _OPERATOR_MESSAGE}}
        assert gate.handle_block_verbatim_operator_paste(payload) is False


class TestUnknownIsAnnouncedNotSilent:
    def test_an_unreadable_transcript_allows_but_says_it_could_not_check(self, tmp_path: Path) -> None:
        blocked, stderr = _run(_PUBLIC_POST, str(tmp_path / "missing.jsonl"))
        assert blocked is False
        assert "could NOT check" in stderr

    def test_the_unknown_outcome_is_recorded(self, tmp_path: Path) -> None:
        _run(_PUBLIC_POST, str(tmp_path / "missing.jsonl"))
        audit = (tmp_path / "state" / "verbatim-paste.jsonl").read_text(encoding="utf-8")
        assert json.loads(audit.strip())["outcome"] == vp.UNKNOWN


class TestUnresolvableBodyIsUnknownNotClean:
    """#4195 review finding: a sentinel-carrying payload must not silently scan clean."""

    _UNRESOLVABLE = (
        'gh issue create --repo souliane/teatree --title "Post-mortem" --body-file /nonexistent/does-not-exist.md'
    )

    def test_a_missing_body_file_is_unknown_not_a_silent_clean(self, said: str) -> None:
        blocked, stderr = _run(self._UNRESOLVABLE, said)
        assert blocked is False
        assert "could NOT check" in stderr

    def test_the_unresolvable_body_outcome_is_recorded_as_unknown(self, said: str, tmp_path: Path) -> None:
        _run(self._UNRESOLVABLE, said)
        audit = (tmp_path / "state" / "verbatim-paste.jsonl").read_text(encoding="utf-8")
        assert json.loads(audit.strip())["outcome"] == vp.UNKNOWN


class TestEscapes:
    def test_a_private_target_resolved_for_real_is_allowed_and_the_variable_never_printed(
        self, said: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Privacy comes from the host-qualified allowlist with the live probe unreachable, as on a cold box.
        db = tmp_path / "config.sqlite3"
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute(
                "CREATE TABLE teatree_config_setting "
                "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'private_repos', ?)",
                (json.dumps(["github.com/acme/private-notes"]),),
            )
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        private_post = _PUBLIC_POST.replace("souliane/teatree", "acme/private-notes")
        out, err = StringIO(), StringIO()

        with patch("sys.stdout", out), patch("sys.stderr", err):
            blocked = gate.handle_block_verbatim_operator_paste(_event(f"ALLOW_VERBATIM_PASTE=1 {private_post}", said))

        assert blocked is False
        assert "ALLOW_VERBATIM_PASTE" not in out.getvalue() + err.getvalue()
        assert _run(f"ALLOW_VERBATIM_PASTE=1 {_PUBLIC_POST}", said)[0] is True

    def test_the_env_prefix_override_cannot_publish_publicly(self, said: str, tmp_path: Path) -> None:
        blocked, stderr = _run(f"ALLOW_VERBATIM_PASTE=1 {_PUBLIC_POST}", said)
        assert blocked is True
        assert "ALLOW_VERBATIM_PASTE" not in stderr
        audit = (tmp_path / "state" / "verbatim-paste.jsonl").read_text(encoding="utf-8")
        assert json.loads(audit.strip())["decision"] == "blocked"

    def test_process_env_override_cannot_publish_publicly(self, said: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALLOW_VERBATIM_PASTE", "1")
        assert _run(_PUBLIC_POST, said)[0] is True

    def test_the_kill_switch_disables_the_gate(self, said: str) -> None:
        with patch.object(gate, "_teatree_bool_setting", return_value=False):
            assert gate.handle_block_verbatim_operator_paste(_event(_PUBLIC_POST, said)) is False

    def test_an_internal_error_fails_open_loudly(self, said: str) -> None:
        err = StringIO()
        with patch("sys.stderr", err), patch.object(gate, "_run_verbatim_paste_pretool", side_effect=RuntimeError("x")):
            blocked = gate.handle_block_verbatim_operator_paste(_event(_PUBLIC_POST, said))
        assert blocked is False
        assert "failed open" in err.getvalue()
        assert "NOT a clean scan" in err.getvalue()


class TestTheOperatorsWordsComeFromTheTranscript:
    def test_only_the_newest_messages_are_history(self, tmp_path: Path) -> None:
        newer = [
            f"filler message number {index} " + " ".join(["pad"] * 10) for index in range(vp.MAX_RECORDED_MESSAGES)
        ]
        assert _run(_PUBLIC_POST, _transcript(tmp_path, _OPERATOR_MESSAGE, *newer))[0] is False
        assert _run(_PUBLIC_POST, _transcript(tmp_path, *newer, _OPERATOR_MESSAGE))[0] is True

    def test_a_bare_loop_tick_is_not_operator_speech(self, tmp_path: Path) -> None:
        transcript = _transcript(tmp_path, LOOP_PROMPT)
        assert (
            _run(
                f"gh issue create --repo souliane/teatree --title t --body '> {LOOP_PROMPT} {LOOP_PROMPT}'", transcript
            )[0]
            is False
        )

    def test_harness_ambient_context_is_not_the_operators_words(self, tmp_path: Path) -> None:
        ambient = f"<system-reminder>{_OPERATOR_MESSAGE}</system-reminder>\nplease continue with the ticket"
        assert _run(_PUBLIC_POST, _transcript(tmp_path, ambient))[0] is False


class TestWiring:
    def test_the_gate_is_registered(self) -> None:
        assert gate.handle_block_verbatim_operator_paste in router._HANDLERS["PreToolUse"]

    def test_the_gate_runs_after_the_banned_terms_gate(self) -> None:
        chain = router._HANDLERS["PreToolUse"]
        assert chain.index(gate.handle_block_verbatim_operator_paste) > chain.index(router.handle_banned_terms_pretool)
