"""The ``all``-mode Stop hook that reads the final reply aloud (#1791).

When ``[teatree] speak_mode = all`` and ``say``/``t3`` are on PATH, the
Stop hook hands the transcript's last assistant text to a detached
``t3 speak`` subprocess. It NEVER blocks/denies (returns None, writes no
stdout JSON), pre-checks the toml setting so only ``all`` triggers it,
and is crash-proof. Only the toml file, PATH lookup, and the detached
subprocess (external boundaries) are faked.
"""

import json
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


def _write_config(tmp_path: Path, speak_mode: str | None) -> None:
    cfg = tmp_path / ".teatree.toml"
    if speak_mode is None:
        cfg.write_text("[teatree]\n", encoding="utf-8")
    else:
        cfg.write_text(f'[teatree]\nspeak_mode = "{speak_mode}"\n', encoding="utf-8")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(router.Path, "home", classmethod(lambda _cls: tmp_path))
    return tmp_path


class TestSpeakModeSetting:
    def test_off_when_file_missing(self, home: Path) -> None:
        assert router._speak_mode_setting() == "off"

    def test_off_when_no_teatree_table(self, home: Path) -> None:
        (home / ".teatree.toml").write_text("[other]\nx = 1\n", encoding="utf-8")
        assert router._speak_mode_setting() == "off"

    def test_reads_all(self, home: Path) -> None:
        _write_config(home, "all")
        assert router._speak_mode_setting() == "all"

    def test_off_on_malformed_toml(self, home: Path) -> None:
        (home / ".teatree.toml").write_text("not = [valid toml", encoding="utf-8")
        assert router._speak_mode_setting() == "off"


class TestHandleSpeakAllOnStop:
    def test_spawns_t3_speak_for_all_mode(self, home: Path, tmp_path: Path) -> None:
        _write_config(home, "all")
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

    def test_appends_overlay_when_set(self, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_config(home, "all")
        monkeypatch.setenv("T3_OVERLAY_NAME", "teatree")
        transcript = _write_transcript(tmp_path, "done")
        with (
            patch.object(router.shutil, "which", return_value="/usr/local/bin/t3"),
            patch.object(router.subprocess, "Popen") as popen,
        ):
            router.handle_speak_all_on_stop({"transcript_path": transcript})
        argv = popen.call_args.args[0]
        assert argv[-2:] == ["--overlay", "teatree"]

    def test_noop_for_im_only_mode(self, home: Path, tmp_path: Path) -> None:
        _write_config(home, "im-only")
        transcript = _write_transcript(tmp_path, "x")
        with (
            patch.object(router.shutil, "which", return_value="/usr/local/bin/t3"),
            patch.object(router.subprocess, "Popen") as popen,
        ):
            router.handle_speak_all_on_stop({"transcript_path": transcript})
        popen.assert_not_called()

    def test_noop_when_off(self, home: Path, tmp_path: Path) -> None:
        _write_config(home, None)
        transcript = _write_transcript(tmp_path, "x")
        with patch.object(router.subprocess, "Popen") as popen:
            router.handle_speak_all_on_stop({"transcript_path": transcript})
        popen.assert_not_called()

    def test_noop_when_say_absent(self, home: Path, tmp_path: Path) -> None:
        _write_config(home, "all")
        transcript = _write_transcript(tmp_path, "x")
        with (
            patch.object(router.shutil, "which", side_effect=lambda b: None if b == "say" else "/bin/t3"),
            patch.object(router.subprocess, "Popen") as popen,
        ):
            router.handle_speak_all_on_stop({"transcript_path": transcript})
        popen.assert_not_called()

    def test_noop_when_t3_absent(self, home: Path, tmp_path: Path) -> None:
        _write_config(home, "all")
        transcript = _write_transcript(tmp_path, "x")
        with (
            patch.object(router.shutil, "which", side_effect=lambda b: "/usr/bin/say" if b == "say" else None),
            patch.object(router.subprocess, "Popen") as popen,
        ):
            router.handle_speak_all_on_stop({"transcript_path": transcript})
        popen.assert_not_called()

    def test_noop_when_no_assistant_text(self, home: Path, tmp_path: Path) -> None:
        _write_config(home, "all")
        empty = tmp_path / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        with (
            patch.object(router.shutil, "which", return_value="/bin/t3"),
            patch.object(router.subprocess, "Popen") as popen,
        ):
            router.handle_speak_all_on_stop({"transcript_path": str(empty)})
        popen.assert_not_called()

    def test_crash_proof_returns_none(self, home: Path) -> None:
        _write_config(home, "all")
        with (
            patch.object(router.shutil, "which", return_value="/bin/t3"),
            patch.object(router, "_last_assistant_turn", side_effect=RuntimeError("boom")),
        ):
            assert router.handle_speak_all_on_stop({"transcript_path": "x"}) is None

    def test_registered_in_stop_chain(self) -> None:
        assert router.handle_speak_all_on_stop in router._HANDLERS["Stop"]
        # Ordered before the self-pump (which short-circuits the chain).
        stop = router._HANDLERS["Stop"]
        assert stop.index(router.handle_speak_all_on_stop) < stop.index(router.handle_loop_self_pump)
