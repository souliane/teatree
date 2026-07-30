"""``t3 speak-dm`` — the detached DM-audio worker for the question mirror (#2171).

The mirror hook spawns this command detached so an AskUserQuestion Slack DM
carries audio to the user's phone. It resolves the overlay's Slack backend and
runs the :func:`teatree.core.speak.deliver_user_dm_sidecar` side-effects.

Acceptance-facing anti-vacuity: with ``speak.slack`` ON, a mocked backend's
``post_audio_dm`` IS called (the phone gets audio); with it OFF, the DM stays
text-only (no ``post_audio_dm``). Only the messaging backend + synthesis are
faked.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

from teatree.core.management.commands.speak_dm import Command
from teatree.types import LocalPlayback, SpeakConfig

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _backend() -> MagicMock:
    backend = MagicMock()
    backend.post_audio_dm.return_value = {"ok": True}
    return backend


class TestSpeakDmCommand:
    def test_command_class_is_importable(self) -> None:
        assert Command.__name__ == "Command"

    def test_slack_on_attaches_audio_to_channel(self, tmp_path: Path) -> None:
        audio = tmp_path / "speech.m4a"
        audio.write_bytes(b"x")
        backend = _backend()
        with (
            patch("teatree.core.management.commands.speak_dm.messaging_from_overlay", return_value=backend),
            patch("teatree.core.speak.resolve_speak", return_value=SpeakConfig(slack=True)),
            patch("teatree.core.speak.synthesise", return_value=audio),
        ):
            call_command("speak_dm", "D-USER", "Ship it?", thread_ts="1700.1")
        backend.post_audio_dm.assert_called_once()
        assert backend.post_audio_dm.call_args.kwargs["channel"] == "D-USER"

    def test_slack_off_is_text_only(self) -> None:
        backend = _backend()
        with (
            patch("teatree.core.management.commands.speak_dm.messaging_from_overlay", return_value=backend),
            patch("teatree.core.speak.resolve_speak", return_value=SpeakConfig(local=LocalPlayback.OFF, slack=False)),
            patch("teatree.core.speak.threading.Thread"),
        ):
            call_command("speak_dm", "D-USER", "Ship it?")
        backend.post_audio_dm.assert_not_called()

    def test_no_backend_is_clean_noop(self) -> None:
        with patch("teatree.core.management.commands.speak_dm.messaging_from_overlay", return_value=None):
            call_command("speak_dm", "D-USER", "hi")  # must not raise

    def test_blank_text_is_noop(self) -> None:
        called = MagicMock()
        with patch("teatree.core.management.commands.speak_dm.messaging_from_overlay", side_effect=called):
            call_command("speak_dm", "D-USER", "   ")
        called.assert_not_called()

    def test_blank_channel_is_noop(self) -> None:
        called = MagicMock()
        with patch("teatree.core.management.commands.speak_dm.messaging_from_overlay", side_effect=called):
            call_command("speak_dm", "", "hi")
        called.assert_not_called()

    def test_overlay_sets_and_restores_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        backend = _backend()
        seen: dict[str, str] = {}

        def _capture(overlay: str | None) -> MagicMock:
            seen["overlay_env"] = os.environ.get("T3_OVERLAY_NAME", "")
            return backend

        with (
            patch("teatree.core.management.commands.speak_dm.messaging_from_overlay", side_effect=_capture),
            patch("teatree.core.speak.resolve_speak", return_value=SpeakConfig(slack=False)),
            patch("teatree.core.speak.threading.Thread"),
        ):
            call_command("speak_dm", "D-USER", "hi", overlay="acme")
        assert seen["overlay_env"] == "acme"  # set during the call
        assert "T3_OVERLAY_NAME" not in os.environ  # restored after

    def test_overlay_restores_prior_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_OVERLAY_NAME", "prior")
        with (
            patch("teatree.core.management.commands.speak_dm.messaging_from_overlay", return_value=None),
            patch("teatree.core.speak.threading.Thread"),
        ):
            call_command("speak_dm", "D-USER", "hi", overlay="acme")
        assert os.environ["T3_OVERLAY_NAME"] == "prior"  # prior value restored, not popped
