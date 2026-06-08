"""The text-to-speech ``speak()`` seam + the shared ``deliver_user_dm`` chokepoint (#2060).

Covers the binary-presence gate (``say`` absent → inert config), the
markdown/code/URL stripping, the local-speakers Stop-hook path, and — the
load-bearing #2060 behaviour — :func:`deliver_user_dm` posting ONE DM that
carries the text + an inline audio attachment (degrading to a text-only
post when synthesis fails). The v3 axes are independent: local play (``local``
dm/all) is never suppressed by the ``slack`` attach. Every unstoppable external
is mocked at the network boundary: the ``say`` / ``afconvert`` subprocesses and
the messaging backend. ``block=True`` runs delivery synchronously so assertions
don't race the daemon thread.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from teatree.core import availability
from teatree.core import speak as speak_mod
from teatree.types import LocalPlayback, SpeakConfig


def _resolution(mode: str) -> availability.Resolution:
    return availability.Resolution(mode=mode, source="override")


class TestBinaryGate:
    def test_binary_available_true_when_on_path(self) -> None:
        with patch.object(speak_mod.shutil, "which", return_value="/usr/bin/say"):
            assert speak_mod.binary_available() is True

    def test_binary_available_false_when_absent(self) -> None:
        with patch.object(speak_mod.shutil, "which", return_value=None):
            assert speak_mod.binary_available() is False

    def test_resolve_speak_forced_inert_when_binary_absent(self) -> None:
        with (
            patch.object(speak_mod, "binary_available", return_value=False),
            patch.object(
                speak_mod,
                "get_effective_settings",
                return_value=MagicMock(speak=SpeakConfig(local=LocalPlayback.ALL, slack=True)),
            ),
        ):
            assert speak_mod.resolve_speak() == SpeakConfig()

    def test_resolve_speak_returns_configured_when_binary_present(self) -> None:
        configured = SpeakConfig(local=LocalPlayback.DM, slack=True)
        with (
            patch.object(speak_mod, "binary_available", return_value=True),
            patch.object(speak_mod, "get_effective_settings", return_value=MagicMock(speak=configured)),
        ):
            assert speak_mod.resolve_speak() == configured


class TestAwayGateResolveSpeak:
    """Away suppresses LOCAL playback while preserving ``slack`` — without touching config."""

    def test_away_forces_local_off_but_preserves_slack(self) -> None:
        with (
            patch.object(speak_mod, "binary_available", return_value=True),
            patch.object(
                speak_mod,
                "get_effective_settings",
                return_value=MagicMock(speak=SpeakConfig(local=LocalPlayback.ALL, slack=True)),
            ),
            patch.object(availability, "resolve_mode", return_value=_resolution(availability.MODE_AWAY)),
        ):
            resolved = speak_mod.resolve_speak()
        assert resolved.local is LocalPlayback.OFF
        assert resolved.slack is True

    def test_present_preserves_local(self) -> None:
        with (
            patch.object(speak_mod, "binary_available", return_value=True),
            patch.object(
                speak_mod,
                "get_effective_settings",
                return_value=MagicMock(speak=SpeakConfig(local=LocalPlayback.ALL, slack=True)),
            ),
            patch.object(availability, "resolve_mode", return_value=_resolution(availability.MODE_PRESENT)),
        ):
            resolved = speak_mod.resolve_speak()
        assert resolved.local is LocalPlayback.ALL
        assert resolved.slack is True

    def test_availability_raising_is_treated_as_present(self) -> None:
        with (
            patch.object(speak_mod, "binary_available", return_value=True),
            patch.object(
                speak_mod,
                "get_effective_settings",
                return_value=MagicMock(speak=SpeakConfig(local=LocalPlayback.ALL, slack=True)),
            ),
            patch.object(availability, "resolve_mode", side_effect=RuntimeError("boom")),
        ):
            resolved = speak_mod.resolve_speak()
        assert resolved.local is LocalPlayback.ALL
        assert resolved.slack is True


class TestAwayGateConsumers:
    """The away-gate silences BOTH local consumers while Slack audio still attaches."""

    def test_away_speak_does_not_play_locally(self) -> None:
        with (
            patch.object(speak_mod, "binary_available", return_value=True),
            patch.object(
                speak_mod,
                "get_effective_settings",
                return_value=MagicMock(speak=SpeakConfig(local=LocalPlayback.ALL, slack=True)),
            ),
            patch.object(availability, "resolve_mode", return_value=_resolution(availability.MODE_AWAY)),
            patch.object(speak_mod, "_speak_local") as local,
        ):
            speak_mod.speak("tests are green", block=True)
        local.assert_not_called()

    def test_away_dm_still_attaches_slack_audio_but_no_local_play(self, tmp_path: Path) -> None:
        audio = tmp_path / "speech.m4a"
        audio.write_bytes(b"x")
        backend = _backend()
        with (
            patch.object(speak_mod, "binary_available", return_value=True),
            patch.object(
                speak_mod,
                "get_effective_settings",
                return_value=MagicMock(speak=SpeakConfig(local=LocalPlayback.ALL, slack=True)),
            ),
            patch.object(availability, "resolve_mode", return_value=_resolution(availability.MODE_AWAY)),
            patch.object(speak_mod, "synthesise", return_value=audio),
            patch.object(speak_mod.threading, "Thread") as thread_cls,
        ):
            speak_mod.deliver_user_dm(backend, channel="D-USER", text="hi")
        backend.post_audio_dm.assert_called_once()
        thread_cls.assert_not_called()

    def test_availability_raising_lets_local_play_slack_unaffected(self, tmp_path: Path) -> None:
        audio = tmp_path / "speech.m4a"
        audio.write_bytes(b"x")
        backend = _backend()
        with (
            patch.object(speak_mod, "binary_available", return_value=True),
            patch.object(
                speak_mod,
                "get_effective_settings",
                return_value=MagicMock(speak=SpeakConfig(local=LocalPlayback.ALL, slack=True)),
            ),
            patch.object(availability, "resolve_mode", side_effect=RuntimeError("boom")),
            patch.object(speak_mod, "synthesise", return_value=audio),
            patch.object(speak_mod.threading, "Thread") as thread_cls,
        ):
            speak_mod.deliver_user_dm(backend, channel="D-USER", text="hi")
        backend.post_audio_dm.assert_called_once()
        thread_cls.assert_called_once()


class TestCleanForSpeech:
    def test_drops_code_fences(self) -> None:
        out = speak_mod.clean_for_speech("before ```python\nx = 1\n``` after")
        assert "x = 1" not in out
        assert "before" in out
        assert "after" in out

    def test_collapses_md_link_to_label(self) -> None:
        out = speak_mod.clean_for_speech("see [the PR](https://example.com/pr/1) please")
        assert "the PR" in out
        assert "example.com" not in out

    def test_caps_length_on_word_boundary(self) -> None:
        out = speak_mod.clean_for_speech("word " * 400)
        assert len(out) <= speak_mod._MAX_SPEAK_CHARS + 1
        assert out.endswith("…")

    def test_blank_after_strip(self) -> None:
        assert speak_mod.clean_for_speech("```only code```") == ""


class TestFiltersStatusLogNoise:
    """Status/log noise is filtered out of spoken text; real prose passes (#277).

    The TTS chokepoint :func:`clean_for_speech` is the ONE place every spoken
    string flows through (the Stop-hook in-client read and the DM local leg).
    A bot->user INFO DM is prefixed with a ``:information_source: *info*`` kind
    marker line (:func:`teatree.core.notify._format`), and assistant turns /
    DM bodies routinely carry log-status lines (``INFO:`` / ``DEBUG`` levels,
    bare emoji-shortcode status markers). Read verbatim these voice as gibberish
    -- ``:information_source:`` reads as "information source", the kind marker as
    "info" -- so ``say`` drones "Info source", "Info test green" before the real
    message. The filter drops those lines and the emoji shortcodes while leaving
    user-facing prose intact.
    """

    def test_drops_emoji_shortcode_status_markers(self) -> None:
        # The literal noise from the bug report: a notify INFO prefix line.
        out = speak_mod.clean_for_speech(":information_source: *info*\ntests are green")
        assert "information" not in out.lower()
        assert "source" not in out.lower()
        assert "tests are green" in out

    def test_drops_kind_marker_prefix_line_keeps_real_message(self) -> None:
        out = speak_mod.clean_for_speech(":information_source: *info*\nsource fetched and applied")
        # The standalone "info" kind marker line is gone, but the real message
        # -- which legitimately contains the word "source" -- is preserved.
        assert "info" not in out.lower().split()
        assert "source fetched and applied" in out

    def test_drops_log_level_lines(self) -> None:
        text = "INFO: source\nDEBUG: cache warm\nThe migration finished cleanly."
        out = speak_mod.clean_for_speech(text)
        assert "source" not in out.lower()
        assert "cache warm" not in out.lower()
        assert "The migration finished cleanly." in out

    def test_drops_bracketed_log_level_lines(self) -> None:
        out = speak_mod.clean_for_speech("[INFO] test green\nDeploy is live for everyone.")
        assert "test green" not in out.lower()
        assert "Deploy is live for everyone." in out

    def test_drops_bare_emoji_status_line(self) -> None:
        out = speak_mod.clean_for_speech(":white_check_mark: test green\nYour review is requested.")
        assert "test green" not in out.lower()
        assert "Your review is requested." in out

    def test_real_message_with_status_words_inline_is_kept(self) -> None:
        # "info"/"source"/"green" appearing INSIDE a prose sentence are NOT noise.
        sentence = "The info you asked for: the source is green and ready."
        out = speak_mod.clean_for_speech(sentence)
        assert out == sentence

    def test_all_noise_collapses_to_empty(self) -> None:
        assert speak_mod.clean_for_speech(":information_source: *info*\nINFO: done") == ""

    def test_level_word_with_separator_is_filtered(self) -> None:
        # A level token followed by a real log discriminator (closing bracket,
        # ``:`` or ``-``) is a genuine log line and stays filtered.
        for noise in (
            "INFO: source",
            "[DEBUG] cache warm",
            "WARNING - low disk",
            "[INFO] test green",
            "Notice: maintenance",
        ):
            out = speak_mod.clean_for_speech(f"{noise}\nThe deploy finished cleanly.")
            assert "The deploy finished cleanly." in out
            assert speak_mod.clean_for_speech(noise) == "", noise

    def test_level_word_without_separator_is_kept_as_prose(self) -> None:
        # A line whose first word merely HAPPENS to be a level token, with no
        # log discriminator after it, is ordinary prose the user wants spoken.
        for prose in (
            "Warning users now about the outage",
            "Error handling was improved in this PR.",
            "Critical bug found in prod.",
        ):
            assert speak_mod.clean_for_speech(prose) == prose, prose


class TestSpeakLocalDispatch:
    """The in-client Stop-hook read: ``speak()`` fires only when ``local == all``."""

    def test_noop_when_local_off(self) -> None:
        with (
            patch.object(speak_mod, "resolve_speak", return_value=SpeakConfig(local=LocalPlayback.OFF, slack=True)),
            patch.object(speak_mod, "_speak_local") as local,
        ):
            speak_mod.speak("anything", block=True)
        local.assert_not_called()

    def test_noop_when_local_dm(self) -> None:
        # local=dm speaks DM texts only, not the in-client turn.
        with (
            patch.object(speak_mod, "resolve_speak", return_value=SpeakConfig(local=LocalPlayback.DM)),
            patch.object(speak_mod, "_speak_local") as local,
        ):
            speak_mod.speak("tests are green", block=True)
        local.assert_not_called()

    def test_fires_when_local_all_regardless_of_slack(self) -> None:
        with (
            patch.object(speak_mod, "resolve_speak", return_value=SpeakConfig(local=LocalPlayback.ALL, slack=True)),
            patch.object(speak_mod, "_speak_local") as local,
        ):
            speak_mod.speak("tests are green", block=True)
        local.assert_called_once_with("tests are green")

    def test_blank_cleaned_text_is_noop(self) -> None:
        with (
            patch.object(speak_mod, "resolve_speak", return_value=SpeakConfig(local=LocalPlayback.ALL)),
            patch.object(speak_mod, "_speak_local") as local,
        ):
            speak_mod.speak("```only code```", block=True)
        local.assert_not_called()

    def test_block_true_runs_local_synchronously(self) -> None:
        with (
            patch.object(speak_mod, "resolve_speak", return_value=SpeakConfig(local=LocalPlayback.ALL)),
            patch.object(speak_mod, "_speak_local") as local,
        ):
            speak_mod.speak("tests are green", block=True)
        local.assert_called_once_with("tests are green")

    def test_block_false_spawns_daemon_thread(self) -> None:
        with (
            patch.object(speak_mod, "resolve_speak", return_value=SpeakConfig(local=LocalPlayback.ALL)),
            patch.object(speak_mod.threading, "Thread") as thread_cls,
        ):
            speak_mod.speak("hi", block=False)
        thread_cls.assert_called_once()
        assert thread_cls.call_args.kwargs["daemon"] is True
        thread_cls.return_value.start.assert_called_once()


def _backend(*, audio_ok: bool = True, audio_error: str = "") -> MagicMock:
    backend = MagicMock()
    backend.post_message.return_value = {"ok": True, "ts": "1.0"}
    body: dict[str, object] = {"ok": audio_ok}
    if audio_error:
        body["error"] = audio_error
    if audio_ok:
        body["ts"] = "1.0"
    backend.post_audio_dm.return_value = body
    return backend


class TestDeliverUserDmAttachAudio:
    """#2060 part 1: ONE DM = text + attached audio (the load-bearing tests)."""

    def test_dm_with_audio_is_one_message_with_initial_comment(self, tmp_path: Path) -> None:
        audio = tmp_path / "speech.m4a"
        audio.write_bytes(b"x")
        backend = _backend()
        with (
            patch.object(speak_mod, "resolve_speak", return_value=SpeakConfig(slack=True)),
            patch.object(speak_mod, "synthesise", return_value=audio),
        ):
            response = speak_mod.deliver_user_dm(
                backend, channel="D-USER", text=":info: tests are green", thread_ts="T1"
            )
        backend.post_audio_dm.assert_called_once()
        kwargs = backend.post_audio_dm.call_args.kwargs
        assert kwargs["channel"] == "D-USER"
        assert kwargs["text"] == ":info: tests are green"
        assert kwargs["thread_ts"] == "T1"
        # The whole point: NO separate text post — the audio rides the text DM.
        backend.post_message.assert_not_called()
        assert response["ok"] is True

    def test_no_standalone_audio_post_then_text_post(self, tmp_path: Path) -> None:
        audio = tmp_path / "speech.m4a"
        audio.write_bytes(b"x")
        backend = _backend()
        with (
            patch.object(speak_mod, "resolve_speak", return_value=SpeakConfig(slack=True)),
            patch.object(speak_mod, "synthesise", return_value=audio),
        ):
            speak_mod.deliver_user_dm(backend, channel="D-USER", text="hi")
        assert backend.post_audio_dm.call_count == 1
        assert backend.post_message.call_count == 0

    def test_audio_dm_threaded(self, tmp_path: Path) -> None:
        audio = tmp_path / "speech.m4a"
        audio.write_bytes(b"x")
        backend = _backend()
        with (
            patch.object(speak_mod, "resolve_speak", return_value=SpeakConfig(slack=True)),
            patch.object(speak_mod, "synthesise", return_value=audio),
        ):
            speak_mod.deliver_user_dm(backend, channel="D-USER", text="hi", thread_ts="1700.0001")
        assert backend.post_audio_dm.call_args.kwargs["thread_ts"] == "1700.0001"

    def test_synth_failure_degrades_to_text_dm(self) -> None:
        backend = _backend()
        with (
            patch.object(speak_mod, "resolve_speak", return_value=SpeakConfig(slack=True)),
            patch.object(speak_mod, "synthesise", return_value=None),
        ):
            response = speak_mod.deliver_user_dm(backend, channel="D-USER", text="hi", thread_ts="T1")
        backend.post_audio_dm.assert_not_called()
        backend.post_message.assert_called_once_with(channel="D-USER", text="hi", thread_ts="T1")
        assert response["ok"] is True

    def test_slack_off_posts_text_only(self) -> None:
        backend = _backend()
        with (
            patch.object(speak_mod, "resolve_speak", return_value=SpeakConfig(local=LocalPlayback.DM, slack=False)),
            patch.object(speak_mod, "synthesise") as synth,
        ):
            speak_mod.deliver_user_dm(backend, channel="D-USER", text="hi")
        synth.assert_not_called()
        backend.post_audio_dm.assert_not_called()
        backend.post_message.assert_called_once()

    def test_missing_files_scope_surfaces_once_and_text_still_delivered(self, tmp_path: Path) -> None:
        audio = tmp_path / "speech.m4a"
        audio.write_bytes(b"x")
        backend = _backend(audio_ok=False, audio_error="missing_scope")
        with (
            patch.object(speak_mod, "resolve_speak", return_value=SpeakConfig(slack=True)),
            patch.object(speak_mod, "synthesise", return_value=audio),
            patch.object(speak_mod, "_surface_upload_failure") as surface,
        ):
            response = speak_mod.deliver_user_dm(backend, channel="D-USER", text="hi")
        surface.assert_called_once_with("missing_scope")
        backend.post_message.assert_called_once()
        assert response["ok"] is True

    def test_local_leg_fires_independently_of_slack_when_local_plays_dms(self, tmp_path: Path) -> None:
        backend = _backend()
        with (
            patch.object(speak_mod, "resolve_speak", return_value=SpeakConfig(local=LocalPlayback.DM, slack=False)),
            patch.object(speak_mod.threading, "Thread") as thread_cls,
        ):
            speak_mod.deliver_user_dm(backend, channel="D-USER", text="play me")
        thread_cls.assert_called_once()
        assert thread_cls.call_args.kwargs["daemon"] is True

    def test_local_leg_fires_under_slack_on_too(self, tmp_path: Path) -> None:
        # v3: the local play is independent of the slack attach — both run.
        audio = tmp_path / "speech.m4a"
        audio.write_bytes(b"x")
        backend = _backend()
        with (
            patch.object(speak_mod, "resolve_speak", return_value=SpeakConfig(local=LocalPlayback.DM, slack=True)),
            patch.object(speak_mod, "synthesise", return_value=audio),
            patch.object(speak_mod.threading, "Thread") as thread_cls,
        ):
            speak_mod.deliver_user_dm(backend, channel="D-USER", text="play me")
        backend.post_audio_dm.assert_called_once()
        thread_cls.assert_called_once()

    def test_no_local_leg_when_local_off(self) -> None:
        backend = _backend()
        with (
            patch.object(speak_mod, "resolve_speak", return_value=SpeakConfig(slack=True)),
            patch.object(speak_mod, "synthesise", return_value=None),
            patch.object(speak_mod.threading, "Thread") as thread_cls,
        ):
            speak_mod.deliver_user_dm(backend, channel="D-USER", text="hi")
        thread_cls.assert_not_called()

    def test_no_db_rows_for_utterances(self, tmp_path: Path) -> None:
        # Exclusivity by construction is STATELESS: deliver_user_dm writes no
        # utterance/dedup model. Assert by the absence of any such model.
        from teatree.core import models  # noqa: PLC0415

        for name in dir(models):
            assert "utterance" not in name.lower()
            assert "spokentext" not in name.lower()


class TestSpeakLocal:
    # Each test pins its own per-test lockfile so the bounded acquire (#2156)
    # never races a concurrent holder on the shared real lockfile and drops the
    # read — these assert the un-contended path.
    def test_runs_say_with_text(self, tmp_path: Path) -> None:
        with (
            patch.object(speak_mod.shutil, "which", return_value="/usr/bin/say"),
            patch.object(speak_mod, "_speaker_lock_path", return_value=tmp_path / "speaker.lock"),
            patch.object(speak_mod, "run_allowed_to_fail") as run,
        ):
            speak_mod._speak_local("hello")
        run.assert_called_once()
        assert run.call_args.args[0] == ["/usr/bin/say", "hello"]
        assert run.call_args.kwargs["expected_codes"] is None

    def test_noop_when_say_absent(self, tmp_path: Path) -> None:
        with (
            patch.object(speak_mod.shutil, "which", return_value=None),
            patch.object(speak_mod, "_speaker_lock_path", return_value=tmp_path / "speaker.lock"),
            patch.object(speak_mod, "run_allowed_to_fail") as run,
        ):
            speak_mod._speak_local("hello")
        run.assert_not_called()

    def test_subprocess_error_is_swallowed(self, tmp_path: Path) -> None:
        with (
            patch.object(speak_mod.shutil, "which", return_value="/usr/bin/say"),
            patch.object(speak_mod, "_speaker_lock_path", return_value=tmp_path / "speaker.lock"),
            patch.object(speak_mod, "run_allowed_to_fail", side_effect=OSError("nope")),
        ):
            speak_mod._speak_local("hello")  # must not raise


class TestSynthesise:
    def test_returns_none_when_say_absent(self) -> None:
        with patch.object(
            speak_mod.shutil, "which", side_effect=lambda b: None if b == "say" else "/usr/bin/afconvert"
        ):
            assert speak_mod.synthesise("hello") is None

    def test_returns_none_when_afconvert_absent(self) -> None:
        with patch.object(
            speak_mod.shutil,
            "which",
            side_effect=lambda b: "/usr/bin/say" if b == "say" else None,
        ):
            assert speak_mod.synthesise("hello") is None

    def test_runs_say_then_afconvert_and_returns_m4a(self) -> None:
        with (
            patch.object(speak_mod.shutil, "which", return_value="/usr/bin/tool"),
            patch.object(speak_mod, "run_checked") as run,
        ):
            out = speak_mod.synthesise("hello")
        assert out is not None
        assert out.name == "speech.m4a"
        assert run.call_count == 2
        import shutil as _shutil  # noqa: PLC0415

        _shutil.rmtree(out.parent, ignore_errors=True)

    def test_synthesis_failure_cleans_up_and_returns_none(self) -> None:
        failure = speak_mod.CommandFailedError(["say"], 1, "", "boom")
        with (
            patch.object(speak_mod.shutil, "which", return_value="/usr/bin/tool"),
            patch.object(speak_mod, "run_checked", side_effect=failure),
        ):
            assert speak_mod.synthesise("hello") is None


class TestSurfaceUploadFailure:
    def test_missing_scope_dm_carries_files_write_hint(self) -> None:
        with patch("teatree.core.notify.notify_user") as notify:
            speak_mod._surface_upload_failure("missing_scope")
        notify.assert_called_once()
        message = notify.call_args.args[0]
        assert "files:write" in message
        assert "t3 setup slack-bot" in message
        assert notify.call_args.kwargs["idempotency_key"] == "speak-upload-failed-missing_scope"

    def test_other_error_dm_has_no_hint_and_per_error_key(self) -> None:
        with patch("teatree.core.notify.notify_user") as notify:
            speak_mod._surface_upload_failure("channel_not_found")
        message = notify.call_args.args[0]
        assert "channel_not_found" in message
        assert "files:write" not in message
        assert notify.call_args.kwargs["idempotency_key"] == "speak-upload-failed-channel_not_found"

    def test_notify_failure_is_swallowed(self) -> None:
        with patch("teatree.core.notify.notify_user", side_effect=RuntimeError("boom")):
            speak_mod._surface_upload_failure("missing_scope")  # must not raise
