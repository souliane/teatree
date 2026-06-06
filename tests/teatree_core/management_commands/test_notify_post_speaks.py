"""``t3 <overlay> notify post`` to the user's own DM reads the text aloud.

The ``notify post`` self-DM short-circuit is a bot→user IM/DM egress —
the same class :func:`teatree.core.notify.notify_user` speaks for under
``speak_mode = im-only`` / ``all``. Before the fix only ``notify send``
(``notify_user``) spoke; ``notify post --channel <self-DM>`` posted the
DM and stayed silent because it bypasses ``notify_user`` entirely and the
speak seam was wired only into ``notify_user``.

Coverage:

*   a self-DM post under ``speak_mode = all`` reaches the ``say`` binary
    (asserted via a fake ``say`` on ``PATH`` that records a marker);
*   a colleague/channel post does NOT speak (a colleague surface is not a
    bot→user IM, so reading it aloud to the user would be wrong);
*   ``speak_mode = off`` is silent on the self-DM path too.

Only the Slack HTTP egress is mocked; the speak seam, the self-DM
classifier, and the CLI plumbing all run for real, with ``say`` shadowed
by a fake on ``PATH`` so no audio plays and the user is never messaged.
"""

import os
import stat
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command

from teatree.backends.slack_bot import SlackBotBackend
from teatree.config import UserSettings
from teatree.types import SpeakMode, SpeakTarget

pytestmark = pytest.mark.django_db

_DM_CHANNEL = "D_ME"
_USER_ID = "U_ME"


def _install_fake_say(bin_dir: Path, marker: Path) -> None:
    """Write a fake ``say`` that records its invocation, then sleeps briefly.

    The sleep models real audio latency: the marker write happens after
    the parent's post has returned, so a dispatch that does not survive
    the egress call would miss it.
    """
    fake = bin_dir / "say"
    fake.write_text(f'#!/bin/sh\nprintf "spoke\\n" >> "{marker}"\nsleep 0.2\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _settings(mode: SpeakMode) -> UserSettings:
    return UserSettings(speak_mode=mode, speak_target=SpeakTarget.LOCAL)


def _backend() -> SlackBotBackend:
    return SlackBotBackend(bot_token="xoxb-test", user_id=_USER_ID, dm_channel_id=_DM_CHANNEL)


def _call(*args: str) -> int:
    out, err = StringIO(), StringIO()
    try:
        call_command(*args, stdout=out, stderr=err)
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def _wait_for(marker: Path, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return
        time.sleep(0.02)


class TestNotifyPostSpeaks:
    def test_self_dm_post_reads_text_aloud_under_speak_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "spoke.txt"
        _install_fake_say(bin_dir, marker)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

        backend = _backend()
        with (
            patch.object(backend, "_post", return_value={"ok": True, "ts": "1.0"}),
            patch(
                "teatree.core.management.commands.notify.messaging_from_overlay",
                lambda *_a, **_k: backend,
            ),
            patch("teatree.core.speak.get_effective_settings", lambda *_a, **_k: _settings(SpeakMode.ALL)),
        ):
            code = _call("notify", "post", "--channel", _DM_CHANNEL, "--text", "hello phone")

        assert code == 0
        _wait_for(marker)
        assert marker.exists(), "self-DM notify post did not invoke `say` (the IM egress was silent)"
        assert "spoke" in marker.read_text()

    def test_colleague_post_does_not_speak(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "spoke.txt"
        _install_fake_say(bin_dir, marker)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

        backend = _backend()
        with (
            patch.object(backend, "_post", return_value={"ok": True, "ts": "1.0"}),
            patch(
                "teatree.core.management.commands.notify.messaging_from_overlay",
                lambda *_a, **_k: backend,
            ),
            patch("teatree.core.speak.get_effective_settings", lambda *_a, **_k: _settings(SpeakMode.ALL)),
            patch(
                "teatree.core.on_behalf_egress.require_on_behalf_approval",
                lambda *, target, action, publish: publish(),
            ),
            patch("teatree.core.on_behalf_egress.notify_user_on_behalf_post", lambda *_a, **_k: None),
        ):
            _call("notify", "post", "--channel", "C_TEAM", "--text", "hi team")

        time.sleep(0.5)
        assert not marker.exists(), "a colleague-surface post must not be read aloud to the user"

    def test_self_dm_post_silent_under_speak_off(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "spoke.txt"
        _install_fake_say(bin_dir, marker)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

        backend = _backend()
        with (
            patch.object(backend, "_post", return_value={"ok": True, "ts": "1.0"}),
            patch(
                "teatree.core.management.commands.notify.messaging_from_overlay",
                lambda *_a, **_k: backend,
            ),
            patch("teatree.core.speak.get_effective_settings", lambda *_a, **_k: _settings(SpeakMode.OFF)),
        ):
            _call("notify", "post", "--channel", _DM_CHANNEL, "--text", "nothing to hear")

        time.sleep(0.5)
        assert not marker.exists(), "speak_mode = off must stay silent on the self-DM post path too"
