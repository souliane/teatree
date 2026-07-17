"""The shared hook-state resolver + once-per-session env-override NOTE.

Pins finding 9 (one resolver for all hook state, no scatter across
``~/.teatree`` / ``~/.cache`` / the data dir) and finding 7 (an env-sourced
``QUOTE_OK=1`` / ``ALLOW_BANNED_TERM=1`` override emits a visible stderr NOTE
once per session rather than silently disabling every publish scan).
"""

from pathlib import Path

import pytest

from teatree.hooks import _hook_state
from teatree.paths import DATA_DIR


class TestHookStateRoot:
    def test_t3_data_dir_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "state"))
        assert _hook_state.hook_state_root() == tmp_path / "state"

    def test_falls_back_to_canonical_data_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_DATA_DIR", raising=False)
        assert _hook_state.hook_state_root() == DATA_DIR


class TestEnvOverrideNote:
    def test_no_session_notes_every_time(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        _hook_state.note_env_override_once("QUOTE_OK")
        _hook_state.note_env_override_once("QUOTE_OK")
        err = capsys.readouterr().err
        assert err.count("QUOTE_OK=1 is set in the process environment") == 2

    def test_session_keyed_notes_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-1")
        _hook_state.note_env_override_once("ALLOW_BANNED_TERM")
        _hook_state.note_env_override_once("ALLOW_BANNED_TERM")
        err = capsys.readouterr().err
        assert err.count("ALLOW_BANNED_TERM=1 is set in the process environment") == 1

    def test_marker_write_failure_still_notes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A state root that is an existing FILE makes the marker mkdir raise; the
        # NOTE must still be emitted (visibility matters more than the dedup).
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file", encoding="utf-8")
        monkeypatch.setattr(_hook_state, "hook_state_root", lambda: blocker / "sub")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-2")
        _hook_state.note_env_override_once("QUOTE_OK")
        assert "QUOTE_OK=1 is set in the process environment" in capsys.readouterr().err
