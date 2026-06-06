"""The Stop hook that reads the in-client turn aloud, with no-double-speak (#2050/#2021).

The Stop-hook arm fires its detached ``t3 speak`` IFF ``scope == all`` AND
``local`` AND NOT ``slack_audio`` — so when ``slack_audio`` is on the canonical
spoken delivery is the DM-with-audio and the Stop hook stands down (exclusivity
by construction, no DB). It NEVER blocks/denies (returns None), reads
``[teatree.speak]`` (with the same legacy map as the config loader), and is
crash-proof. Only the toml file, PATH lookup, and the detached subprocess are
faked.
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


def _write_speak_table(tmp_path: Path, body: str) -> None:
    (tmp_path / ".teatree.toml").write_text(body, encoding="utf-8")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(router.Path, "home", classmethod(lambda _cls: tmp_path))
    return tmp_path


class TestSpeakSettings:
    def test_defaults_when_file_missing(self, home: Path) -> None:
        assert router._speak_settings() == (False, False, "dm")

    def test_defaults_when_no_teatree_table(self, home: Path) -> None:
        (home / ".teatree.toml").write_text("[other]\nx = 1\n", encoding="utf-8")
        assert router._speak_settings() == (False, False, "dm")

    def test_reads_new_table(self, home: Path) -> None:
        _write_speak_table(home, '[teatree.speak]\nlocal = true\nslack_audio = true\nscope = "all"\n')
        assert router._speak_settings() == (True, True, "all")

    def test_legacy_im_only_both_maps(self, home: Path) -> None:
        _write_speak_table(home, '[teatree]\nspeak_mode = "im-only"\nspeak_target = "both"\n')
        assert router._speak_settings() == (True, True, "dm")

    def test_legacy_all_local_maps(self, home: Path) -> None:
        _write_speak_table(home, '[teatree]\nspeak_mode = "all"\nspeak_target = "local"\n')
        assert router._speak_settings() == (True, False, "all")

    def test_legacy_off_maps_both_destinations_false(self, home: Path) -> None:
        _write_speak_table(home, '[teatree]\nspeak_mode = "off"\nspeak_target = "both"\n')
        assert router._speak_settings() == (False, False, "dm")

    def test_defaults_on_malformed_toml(self, home: Path) -> None:
        (home / ".teatree.toml").write_text("not = [valid toml", encoding="utf-8")
        assert router._speak_settings() == (False, False, "dm")


class TestHandleSpeakAllOnStop:
    def test_fires_when_scope_all_local_and_not_slack_audio(self, home: Path, tmp_path: Path) -> None:
        _write_speak_table(home, '[teatree.speak]\nlocal = true\nslack_audio = false\nscope = "all"\n')
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

    def test_suppressed_when_slack_audio_on(self, home: Path, tmp_path: Path) -> None:
        # #2021 no-double-speak: slack_audio on → the DM carries the canonical
        # audio, so the Stop hook must NOT also read the same content on the
        # speakers. RED on the pre-#2050 code (which only checked speak_mode==all).
        _write_speak_table(home, '[teatree.speak]\nlocal = true\nslack_audio = true\nscope = "all"\n')
        transcript = _write_transcript(tmp_path, "all green")
        with (
            patch.object(router.shutil, "which", return_value="/usr/local/bin/t3"),
            patch.object(router.subprocess, "Popen") as popen,
        ):
            router.handle_speak_all_on_stop({"transcript_path": transcript})
        popen.assert_not_called()

    def test_silent_when_scope_dm(self, home: Path, tmp_path: Path) -> None:
        _write_speak_table(home, '[teatree.speak]\nlocal = true\nslack_audio = false\nscope = "dm"\n')
        transcript = _write_transcript(tmp_path, "x")
        with (
            patch.object(router.shutil, "which", return_value="/usr/local/bin/t3"),
            patch.object(router.subprocess, "Popen") as popen,
        ):
            router.handle_speak_all_on_stop({"transcript_path": transcript})
        popen.assert_not_called()

    def test_silent_when_local_off(self, home: Path, tmp_path: Path) -> None:
        _write_speak_table(home, '[teatree.speak]\nlocal = false\nslack_audio = false\nscope = "all"\n')
        transcript = _write_transcript(tmp_path, "x")
        with (
            patch.object(router.shutil, "which", return_value="/usr/local/bin/t3"),
            patch.object(router.subprocess, "Popen") as popen,
        ):
            router.handle_speak_all_on_stop({"transcript_path": transcript})
        popen.assert_not_called()

    def test_appends_overlay_when_set(self, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_speak_table(home, '[teatree.speak]\nlocal = true\nscope = "all"\n')
        monkeypatch.setenv("T3_OVERLAY_NAME", "teatree")
        transcript = _write_transcript(tmp_path, "done")
        with (
            patch.object(router.shutil, "which", return_value="/usr/local/bin/t3"),
            patch.object(router.subprocess, "Popen") as popen,
        ):
            router.handle_speak_all_on_stop({"transcript_path": transcript})
        argv = popen.call_args.args[0]
        assert argv[-2:] == ["--overlay", "teatree"]

    def test_noop_when_say_absent(self, home: Path, tmp_path: Path) -> None:
        _write_speak_table(home, '[teatree.speak]\nlocal = true\nscope = "all"\n')
        transcript = _write_transcript(tmp_path, "x")
        with (
            patch.object(router.shutil, "which", side_effect=lambda b: None if b == "say" else "/bin/t3"),
            patch.object(router.subprocess, "Popen") as popen,
        ):
            router.handle_speak_all_on_stop({"transcript_path": transcript})
        popen.assert_not_called()

    def test_noop_when_t3_absent(self, home: Path, tmp_path: Path) -> None:
        _write_speak_table(home, '[teatree.speak]\nlocal = true\nscope = "all"\n')
        transcript = _write_transcript(tmp_path, "x")
        with (
            patch.object(router.shutil, "which", side_effect=lambda b: "/usr/bin/say" if b == "say" else None),
            patch.object(router.subprocess, "Popen") as popen,
        ):
            router.handle_speak_all_on_stop({"transcript_path": transcript})
        popen.assert_not_called()

    def test_noop_when_no_assistant_text(self, home: Path, tmp_path: Path) -> None:
        _write_speak_table(home, '[teatree.speak]\nlocal = true\nscope = "all"\n')
        empty = tmp_path / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        with (
            patch.object(router.shutil, "which", return_value="/bin/t3"),
            patch.object(router.subprocess, "Popen") as popen,
        ):
            router.handle_speak_all_on_stop({"transcript_path": str(empty)})
        popen.assert_not_called()

    def test_crash_proof_returns_none(self, home: Path) -> None:
        _write_speak_table(home, '[teatree.speak]\nlocal = true\nscope = "all"\n')
        with (
            patch.object(router.shutil, "which", return_value="/bin/t3"),
            patch.object(router, "_last_assistant_turn", side_effect=RuntimeError("boom")),
        ):
            assert router.handle_speak_all_on_stop({"transcript_path": "x"}) is None

    def test_registered_in_stop_chain(self) -> None:
        assert router.handle_speak_all_on_stop in router._HANDLERS["Stop"]
        stop = router._HANDLERS["Stop"]
        assert stop.index(router.handle_speak_all_on_stop) < stop.index(router.handle_loop_self_pump)
