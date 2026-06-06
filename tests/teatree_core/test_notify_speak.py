"""``notify_user`` delivers the bot→user DM through the shared speak chokepoint (#2050).

The IM/DM arm of text-to-speech: a successful bot→user DM goes through
:func:`teatree.core.speak.deliver_user_dm`, which posts ONE message
carrying the text + attached audio when ``slack_audio`` is on (and reads it
locally when ``local`` is on), degrading to a text-only post otherwise.
Speaking/attaching must never break or block the notification path. Only
the messaging backend (network boundary) and the synthesis seam are mocked.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.notify import NotifyKind, notify_user
from teatree.types import SpeakConfig


def _backend() -> MagicMock:
    b = MagicMock()
    b.open_dm.return_value = "D-USER"
    b.post_message.return_value = {"ok": True, "ts": "1700000000.000000"}
    b.post_audio_dm.return_value = {"ok": True, "ts": "1700000000.000001"}
    b.get_permalink.return_value = "https://acme.slack.com/archives/D-USER/p1700000000000000"
    return b


class TestNotifyUserSpeaks(TestCase):
    def test_text_only_when_speak_disabled(self) -> None:
        backend = _backend()
        with patch("teatree.core.speak.resolve_speak", return_value=SpeakConfig()):
            sent = notify_user(
                "tests are green",
                kind=NotifyKind.INFO,
                idempotency_key="speak-off",
                backend=backend,
                user_id="U_ME",
            )
        assert sent is True
        backend.post_message.assert_called_once()
        backend.post_audio_dm.assert_not_called()

    def test_attaches_audio_to_the_dm_when_slack_audio_on(self) -> None:
        backend = _backend()
        with (
            patch("teatree.core.speak.resolve_speak", return_value=SpeakConfig(slack_audio=True)),
            patch("teatree.core.speak.synthesise", return_value=__import__("pathlib").Path("/tmp/x/speech.m4a")),
            patch("teatree.core.speak.shutil.rmtree"),
        ):
            sent = notify_user(
                "tests are green",
                kind=NotifyKind.INFO,
                idempotency_key="speak-audio",
                backend=backend,
                user_id="U_ME",
            )
        assert sent is True
        backend.post_audio_dm.assert_called_once()
        backend.post_message.assert_not_called()

    def test_does_not_speak_when_delivery_fails(self) -> None:
        backend = _backend()
        backend.post_message.return_value = {"ok": False, "error": "channel_not_found"}
        with patch("teatree.core.speak.resolve_speak", return_value=SpeakConfig()):
            sent = notify_user(
                "tests are green",
                kind=NotifyKind.INFO,
                idempotency_key="speak-on-failure",
                backend=backend,
                user_id="U_ME",
            )
        assert sent is False

    def test_speak_config_failure_degrades_to_text_dm(self) -> None:
        backend = _backend()
        with patch("teatree.core.speak.resolve_speak", side_effect=RuntimeError("audio boom")):
            sent = notify_user(
                "tests are green",
                kind=NotifyKind.INFO,
                idempotency_key="speak-raises",
                backend=backend,
                user_id="U_ME",
            )
        assert sent is True
        backend.post_message.assert_called_once()
