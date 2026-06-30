"""The Stop hook that reads the in-client turn aloud (#2060).

The Stop-hook arm fires its detached ``t3 speak`` IFF ``local == all`` — in-client
turns are never Slack messages, so the ``slack`` attach is irrelevant and the v2
no-double-speak suppression is gone (``local == all`` speaks the turn regardless
of ``slack``). It NEVER blocks/denies (returns None), reads the DB-home ``speak``
config via the Django-free ``cold_reader`` (eliminate-~/.teatree.toml — a
``[teatree.speak]`` TOML value is ignored on read), and is crash-proof. Only the
sandboxed sqlite, PATH lookup, and the detached subprocess are faked.
"""

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router


def _write_transcript(tmp_path: Path, assistant_text: str) -> str:
    path = tmp_path / "transcript.jsonl"
    lines = [
        {"message": {"role": "user", "content": [{"type": "text", "text": "do it"}]}},
        {"message": {"role": "assistant", "content": [{"type": "text", "text": assistant_text}]}},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return str(path)


def _seed_speak_db(db: Path, value: object | None) -> None:
    """Build a ``teatree_config_setting`` sqlite carrying a GLOBAL ``speak`` row (or none)."""
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', "
            "key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        if value is not None:
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'speak', ?)",
                (json.dumps(value),),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def seed_speak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[object | None], None]:
    """Point the cold reader at a sandboxed sqlite; return a setter for the global ``speak`` value.

    Not calling the setter leaves the DB file ABSENT, so the cold reader fails open
    to the defaults — the "no config" case. ``conftest._isolate_env`` already
    sandboxes HOME and clears ``XDG_DATA_HOME``.
    """
    db = tmp_path / "db.sqlite3"
    monkeypatch.setenv("T3_CONFIG_DB", str(db))

    def _set(value: object | None) -> None:
        if db.exists():
            db.unlink()
        _seed_speak_db(db, value)

    return _set


class TestSpeakSettings:
    def test_defaults_when_db_absent(self, seed_speak: Callable[[object | None], None]) -> None:
        # No sqlite at all (setter never called) → cold reader fails open to defaults.
        assert router._speak_settings() == ("off", False)

    def test_defaults_when_no_speak_row(self, seed_speak: Callable[[object | None], None]) -> None:
        seed_speak(None)  # table exists, no ``speak`` row
        assert router._speak_settings() == ("off", False)

    def test_reads_speak_db_row(self, seed_speak: Callable[[object | None], None]) -> None:
        seed_speak({"local": "all", "slack": True})
        assert router._speak_settings() == ("all", True)

    def test_partial_value_defaults_the_rest(self, seed_speak: Callable[[object | None], None]) -> None:
        seed_speak({"slack": True})
        assert router._speak_settings() == ("off", True)

    def test_non_dict_value_defaults(self, seed_speak: Callable[[object | None], None]) -> None:
        # A corrupt non-dict ``speak`` value degrades to the defaults, never raising.
        seed_speak("all")
        assert router._speak_settings() == ("off", False)


class TestHandleSpeakAllOnStop:
    def test_fires_when_local_all(self, seed_speak: Callable[[object | None], None], tmp_path: Path) -> None:
        seed_speak({"local": "all"})
        transcript = _write_transcript(tmp_path, "all green, shipping now")
        with (
            patch.object(router.shutil, "which", return_value="/usr/local/bin/t3"),
            patch.object(router.subprocess, "Popen") as popen,
        ):
            verdict = router.handle_speak_all_on_stop({"transcript_path": transcript})
        assert verdict is None
        popen.assert_called_once()
        argv = popen.call_args.args[0]
        assert argv[:2] == ["/usr/local/bin/t3", "speak"]
        assert argv[2] == "all green, shipping now"
        assert popen.call_args.kwargs["start_new_session"] is True

    def test_fires_when_local_all_even_with_slack_on(
        self, seed_speak: Callable[[object | None], None], tmp_path: Path
    ) -> None:
        seed_speak({"local": "all", "slack": True})
        transcript = _write_transcript(tmp_path, "all green")
        with (
            patch.object(router.shutil, "which", return_value="/usr/local/bin/t3"),
            patch.object(router.subprocess, "Popen") as popen,
        ):
            router.handle_speak_all_on_stop({"transcript_path": transcript})
        popen.assert_called_once()

    def test_silent_when_local_dm(self, seed_speak: Callable[[object | None], None], tmp_path: Path) -> None:
        seed_speak({"local": "dm"})
        transcript = _write_transcript(tmp_path, "x")
        with (
            patch.object(router.shutil, "which", return_value="/usr/local/bin/t3"),
            patch.object(router.subprocess, "Popen") as popen,
        ):
            router.handle_speak_all_on_stop({"transcript_path": transcript})
        popen.assert_not_called()

    def test_silent_when_local_off(self, seed_speak: Callable[[object | None], None], tmp_path: Path) -> None:
        seed_speak({"local": "off", "slack": True})
        transcript = _write_transcript(tmp_path, "x")
        with (
            patch.object(router.shutil, "which", return_value="/usr/local/bin/t3"),
            patch.object(router.subprocess, "Popen") as popen,
        ):
            router.handle_speak_all_on_stop({"transcript_path": transcript})
        popen.assert_not_called()

    def test_appends_overlay_when_set(
        self, seed_speak: Callable[[object | None], None], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_speak({"local": "all"})
        monkeypatch.setenv("T3_OVERLAY_NAME", "teatree")
        transcript = _write_transcript(tmp_path, "done")
        with (
            patch.object(router.shutil, "which", return_value="/usr/local/bin/t3"),
            patch.object(router.subprocess, "Popen") as popen,
        ):
            router.handle_speak_all_on_stop({"transcript_path": transcript})
        argv = popen.call_args.args[0]
        assert argv[-2:] == ["--overlay", "teatree"]

    def test_noop_when_say_absent(self, seed_speak: Callable[[object | None], None], tmp_path: Path) -> None:
        seed_speak({"local": "all"})
        transcript = _write_transcript(tmp_path, "x")
        with (
            patch.object(router.shutil, "which", side_effect=lambda b: None if b == "say" else "/bin/t3"),
            patch.object(router.subprocess, "Popen") as popen,
        ):
            router.handle_speak_all_on_stop({"transcript_path": transcript})
        popen.assert_not_called()

    def test_noop_when_t3_absent(self, seed_speak: Callable[[object | None], None], tmp_path: Path) -> None:
        seed_speak({"local": "all"})
        transcript = _write_transcript(tmp_path, "x")
        with (
            patch.object(router.shutil, "which", side_effect=lambda b: "/usr/bin/say" if b == "say" else None),
            patch.object(router.subprocess, "Popen") as popen,
        ):
            router.handle_speak_all_on_stop({"transcript_path": transcript})
        popen.assert_not_called()

    def test_noop_when_no_assistant_text(self, seed_speak: Callable[[object | None], None], tmp_path: Path) -> None:
        seed_speak({"local": "all"})
        empty = tmp_path / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        with (
            patch.object(router.shutil, "which", return_value="/bin/t3"),
            patch.object(router.subprocess, "Popen") as popen,
        ):
            router.handle_speak_all_on_stop({"transcript_path": str(empty)})
        popen.assert_not_called()

    def test_crash_proof_returns_none(self, seed_speak: Callable[[object | None], None]) -> None:
        seed_speak({"local": "all"})
        with (
            patch.object(router.shutil, "which", return_value="/bin/t3"),
            patch.object(router, "_last_assistant_turn", side_effect=RuntimeError("boom")),
        ):
            assert router.handle_speak_all_on_stop({"transcript_path": "x"}) is None

    def test_registered_in_stop_chain(self) -> None:
        assert router.handle_speak_all_on_stop in router._HANDLERS["Stop"]
        stop = router._HANDLERS["Stop"]
        assert stop.index(router.handle_speak_all_on_stop) < stop.index(router.handle_loop_self_pump)
