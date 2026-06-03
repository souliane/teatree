"""The local text-to-speech ``speak()`` seam (#1791).

Covers the binary-presence gate (``say`` absent → effective mode ``off``),
the markdown/code/URL stripping + length cap, and the delivery legs for
each :class:`SpeakTarget`. Every unstoppable external is mocked: the
``say`` / ``afconvert`` subprocesses and the Slack upload backend.
``block=True`` runs delivery synchronously so assertions don't race the
daemon thread.
"""

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from teatree.config import UserSettings
from teatree.core import speak as speak_mod
from teatree.types import SpeakMode, SpeakTarget


def _settings(*, speak_mode: SpeakMode, speak_target: SpeakTarget) -> UserSettings:
    return replace(UserSettings(), speak_mode=speak_mode, speak_target=speak_target)


class TestBinaryGate:
    def test_binary_available_true_when_on_path(self) -> None:
        with patch.object(speak_mod.shutil, "which", return_value="/usr/bin/say"):
            assert speak_mod.binary_available() is True

    def test_binary_available_false_when_absent(self) -> None:
        with patch.object(speak_mod.shutil, "which", return_value=None):
            assert speak_mod.binary_available() is False

    def test_resolve_mode_forced_off_when_binary_absent(self) -> None:
        with (
            patch.object(speak_mod, "binary_available", return_value=False),
            patch.object(
                speak_mod,
                "get_effective_settings",
                return_value=_settings(speak_mode=SpeakMode.ALL, speak_target=SpeakTarget.LOCAL),
            ),
        ):
            assert speak_mod.resolve_mode() is SpeakMode.OFF

    def test_resolve_mode_returns_configured_when_binary_present(self) -> None:
        with (
            patch.object(speak_mod, "binary_available", return_value=True),
            patch.object(
                speak_mod,
                "get_effective_settings",
                return_value=_settings(speak_mode=SpeakMode.IM_ONLY, speak_target=SpeakTarget.LOCAL),
            ),
        ):
            assert speak_mod.resolve_mode() is SpeakMode.IM_ONLY


class TestCleanForSpeech:
    def test_drops_code_fences(self) -> None:
        out = speak_mod.clean_for_speech("before ```python\nx = 1\n``` after")
        assert "x = 1" not in out
        assert "before" in out
        assert "after" in out

    def test_drops_inline_code(self) -> None:
        assert "raise SystemExit" not in speak_mod.clean_for_speech("call `raise SystemExit` now")

    def test_collapses_md_link_to_label(self) -> None:
        out = speak_mod.clean_for_speech("see [the PR](https://example.com/pr/1) please")
        assert "the PR" in out
        assert "example.com" not in out

    def test_drops_bare_url(self) -> None:
        assert "http" not in speak_mod.clean_for_speech("done https://example.com/x done")

    def test_strips_heading_bullet_and_emphasis(self) -> None:
        out = speak_mod.clean_for_speech("## Title\n- *bold* item\n> quote")
        assert "#" not in out
        assert "*" not in out
        assert ">" not in out
        assert "Title" in out
        assert "bold" in out

    def test_caps_length_on_word_boundary(self) -> None:
        out = speak_mod.clean_for_speech("word " * 400)
        assert len(out) <= speak_mod._MAX_SPEAK_CHARS + 1
        assert out.endswith("…")

    def test_blank_after_strip(self) -> None:
        assert speak_mod.clean_for_speech("```only code```") == ""


class TestSpeakDispatch:
    def test_off_mode_is_noop(self) -> None:
        with (
            patch.object(speak_mod, "resolve_mode", return_value=SpeakMode.OFF),
            patch.object(speak_mod, "_deliver") as deliver,
        ):
            speak_mod.speak("anything", block=True)
        deliver.assert_not_called()

    def test_blank_cleaned_text_is_noop(self) -> None:
        with (
            patch.object(speak_mod, "resolve_mode", return_value=SpeakMode.ALL),
            patch.object(
                speak_mod,
                "get_effective_settings",
                return_value=_settings(speak_mode=SpeakMode.ALL, speak_target=SpeakTarget.LOCAL),
            ),
            patch.object(speak_mod, "_deliver") as deliver,
        ):
            speak_mod.speak("```only code```", block=True)
        deliver.assert_not_called()

    def test_block_true_runs_delivery_synchronously(self) -> None:
        with (
            patch.object(speak_mod, "resolve_mode", return_value=SpeakMode.ALL),
            patch.object(
                speak_mod,
                "get_effective_settings",
                return_value=_settings(speak_mode=SpeakMode.ALL, speak_target=SpeakTarget.BOTH),
            ),
            patch.object(speak_mod, "_deliver") as deliver,
        ):
            speak_mod.speak("tests are green", block=True)
        deliver.assert_called_once()
        assert deliver.call_args.args[0] == "tests are green"
        assert deliver.call_args.args[1] is SpeakTarget.BOTH

    def test_block_false_spawns_daemon_thread(self) -> None:
        with (
            patch.object(speak_mod, "resolve_mode", return_value=SpeakMode.IM_ONLY),
            patch.object(
                speak_mod,
                "get_effective_settings",
                return_value=_settings(speak_mode=SpeakMode.IM_ONLY, speak_target=SpeakTarget.LOCAL),
            ),
            patch.object(speak_mod.threading, "Thread") as thread_cls,
        ):
            speak_mod.speak("hi", block=False)
        thread_cls.assert_called_once()
        assert thread_cls.call_args.kwargs["daemon"] is True
        thread_cls.return_value.start.assert_called_once()


class TestDeliver:
    def test_local_only_calls_say_not_slack(self) -> None:
        with (
            patch.object(speak_mod, "_speak_local") as local,
            patch.object(speak_mod, "_synthesise_m4a") as synth,
            patch.object(speak_mod, "_upload_to_slack") as upload,
        ):
            speak_mod._deliver("hello", SpeakTarget.LOCAL)
        local.assert_called_once_with("hello")
        synth.assert_not_called()
        upload.assert_not_called()

    def test_slack_only_synthesises_and_uploads(self, tmp_path: Path) -> None:
        audio = tmp_path / "speech.m4a"
        audio.write_bytes(b"x")
        with (
            patch.object(speak_mod, "_speak_local") as local,
            patch.object(speak_mod, "_synthesise_m4a", return_value=audio) as synth,
            patch.object(speak_mod, "_upload_to_slack") as upload,
        ):
            speak_mod._deliver("hello", SpeakTarget.SLACK_AUDIO)
        local.assert_not_called()
        synth.assert_called_once_with("hello")
        upload.assert_called_once_with(audio)
        assert not audio.exists()  # temp dir cleaned

    def test_both_runs_both_legs(self, tmp_path: Path) -> None:
        audio = tmp_path / "speech.m4a"
        audio.write_bytes(b"x")
        with (
            patch.object(speak_mod, "_speak_local") as local,
            patch.object(speak_mod, "_synthesise_m4a", return_value=audio),
            patch.object(speak_mod, "_upload_to_slack") as upload,
        ):
            speak_mod._deliver("hello", SpeakTarget.BOTH)
        local.assert_called_once()
        upload.assert_called_once()

    def test_slack_leg_skipped_when_synthesis_returns_none(self) -> None:
        with (
            patch.object(speak_mod, "_synthesise_m4a", return_value=None),
            patch.object(speak_mod, "_upload_to_slack") as upload,
        ):
            speak_mod._deliver("hello", SpeakTarget.SLACK_AUDIO)
        upload.assert_not_called()

    def test_delivery_failure_is_swallowed(self) -> None:
        with patch.object(speak_mod, "_speak_local", side_effect=RuntimeError("boom")):
            speak_mod._deliver("hello", SpeakTarget.LOCAL)  # must not raise


class TestSpeakLocal:
    def test_runs_say_with_text(self) -> None:
        with (
            patch.object(speak_mod.shutil, "which", return_value="/usr/bin/say"),
            patch.object(speak_mod, "run_allowed_to_fail") as run,
        ):
            speak_mod._speak_local("hello")
        run.assert_called_once()
        assert run.call_args.args[0] == ["/usr/bin/say", "hello"]
        assert run.call_args.kwargs["expected_codes"] is None

    def test_noop_when_say_absent(self) -> None:
        with (
            patch.object(speak_mod.shutil, "which", return_value=None),
            patch.object(speak_mod, "run_allowed_to_fail") as run,
        ):
            speak_mod._speak_local("hello")
        run.assert_not_called()

    def test_subprocess_error_is_swallowed(self) -> None:
        with (
            patch.object(speak_mod.shutil, "which", return_value="/usr/bin/say"),
            patch.object(speak_mod, "run_allowed_to_fail", side_effect=OSError("nope")),
        ):
            speak_mod._speak_local("hello")  # must not raise


class TestSynthesiseM4a:
    def test_returns_none_when_say_absent(self) -> None:
        with patch.object(
            speak_mod.shutil, "which", side_effect=lambda b: None if b == "say" else "/usr/bin/afconvert"
        ):
            assert speak_mod._synthesise_m4a("hello") is None

    def test_returns_none_when_afconvert_absent(self) -> None:
        with patch.object(
            speak_mod.shutil,
            "which",
            side_effect=lambda b: "/usr/bin/say" if b == "say" else None,
        ):
            assert speak_mod._synthesise_m4a("hello") is None

    def test_runs_say_then_afconvert_and_returns_m4a(self) -> None:
        with (
            patch.object(speak_mod.shutil, "which", return_value="/usr/bin/tool"),
            patch.object(speak_mod, "run_checked") as run,
        ):
            out = speak_mod._synthesise_m4a("hello")
        assert out is not None
        assert out.name == "speech.m4a"
        assert run.call_count == 2
        say_argv, afconvert_argv = run.call_args_list[0].args[0], run.call_args_list[1].args[0]
        assert "-o" in say_argv
        assert afconvert_argv[0] == "/usr/bin/tool"
        # clean up the real temp dir the function created
        import shutil as _shutil  # noqa: PLC0415

        _shutil.rmtree(out.parent, ignore_errors=True)

    def test_synthesis_failure_cleans_up_and_returns_none(self) -> None:
        failure = speak_mod.CommandFailedError(["say"], 1, "", "boom")
        with (
            patch.object(speak_mod.shutil, "which", return_value="/usr/bin/tool"),
            patch.object(speak_mod, "run_checked", side_effect=failure),
        ):
            assert speak_mod._synthesise_m4a("hello") is None


class TestUploadToSlack:
    def _backend(self, *, ok: bool = True, error: str = "") -> MagicMock:
        backend = MagicMock()
        backend.open_dm.return_value = "D-USER"
        body = {"ok": ok}
        if error:
            body["error"] = error
        backend.upload_audio_to_dm.return_value = body
        return backend

    def test_uploads_to_resolved_dm(self, tmp_path: Path) -> None:
        audio = tmp_path / "speech.m4a"
        audio.write_bytes(b"x")
        backend = self._backend()
        with (
            patch("teatree.core.backend_factory.messaging_from_overlay", return_value=backend),
            patch("teatree.core.notify._resolve_user_id", return_value="U_ME"),
        ):
            speak_mod._upload_to_slack(audio)
        backend.open_dm.assert_called_once_with("U_ME")
        backend.upload_audio_to_dm.assert_called_once()
        assert backend.upload_audio_to_dm.call_args.kwargs["channel"] == "D-USER"
        assert backend.upload_audio_to_dm.call_args.kwargs["filepath"] == str(audio)

    def test_noop_when_no_backend(self, tmp_path: Path) -> None:
        audio = tmp_path / "speech.m4a"
        audio.write_bytes(b"x")
        with (
            patch("teatree.core.backend_factory.messaging_from_overlay", return_value=None),
            patch("teatree.core.notify._resolve_user_id", return_value="U_ME"),
        ):
            speak_mod._upload_to_slack(audio)  # must not raise

    def test_noop_when_no_user_id(self, tmp_path: Path) -> None:
        audio = tmp_path / "speech.m4a"
        audio.write_bytes(b"x")
        backend = self._backend()
        with (
            patch("teatree.core.backend_factory.messaging_from_overlay", return_value=backend),
            patch("teatree.core.notify._resolve_user_id", return_value=""),
        ):
            speak_mod._upload_to_slack(audio)
        backend.upload_audio_to_dm.assert_not_called()

    def test_noop_when_open_dm_empty(self, tmp_path: Path) -> None:
        audio = tmp_path / "speech.m4a"
        audio.write_bytes(b"x")
        backend = self._backend()
        backend.open_dm.return_value = ""
        with (
            patch("teatree.core.backend_factory.messaging_from_overlay", return_value=backend),
            patch("teatree.core.notify._resolve_user_id", return_value="U_ME"),
        ):
            speak_mod._upload_to_slack(audio)
        backend.upload_audio_to_dm.assert_not_called()

    def test_missing_scope_body_is_logged_not_raised(self, tmp_path: Path) -> None:
        audio = tmp_path / "speech.m4a"
        audio.write_bytes(b"x")
        backend = self._backend(ok=False, error="missing_scope")
        with (
            patch("teatree.core.backend_factory.messaging_from_overlay", return_value=backend),
            patch("teatree.core.notify._resolve_user_id", return_value="U_ME"),
        ):
            speak_mod._upload_to_slack(audio)  # must not raise
        backend.upload_audio_to_dm.assert_called_once()
