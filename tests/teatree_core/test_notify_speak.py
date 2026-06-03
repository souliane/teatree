"""``notify_user`` speaks the IM/DM egress text when ``speak_mode`` is on (#1791).

The IM/DM arm of the text-to-speech feature: a successful bot→user DM
calls :func:`teatree.core.speak.speak` with the egressed text (the seam
itself refuses ``off`` / the binary-absent case). Speaking must never
break or block the notification path.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.notify import NotifyKind, notify_user


def _backend() -> MagicMock:
    b = MagicMock()
    b.open_dm.return_value = "D-USER"
    b.post_message.return_value = {"ok": True, "ts": "1700000000.000000"}
    b.get_permalink.return_value = "https://acme.slack.com/archives/D-USER/p1700000000000000"
    return b


class TestNotifyUserSpeaks(TestCase):
    def test_speaks_the_delivered_text_on_success(self) -> None:
        with patch("teatree.core.speak.speak") as speak:
            sent = notify_user(
                "tests are green",
                kind=NotifyKind.INFO,
                idempotency_key="speak-on-success",
                backend=_backend(),
                user_id="U_ME",
            )
        assert sent is True
        speak.assert_called_once_with("tests are green")

    def test_does_not_speak_when_delivery_fails(self) -> None:
        backend = _backend()
        backend.post_message.return_value = {"ok": False, "error": "channel_not_found"}
        with patch("teatree.core.speak.speak") as speak:
            sent = notify_user(
                "tests are green",
                kind=NotifyKind.INFO,
                idempotency_key="speak-on-failure",
                backend=backend,
                user_id="U_ME",
            )
        assert sent is False
        speak.assert_not_called()

    def test_speak_failure_does_not_break_notify(self) -> None:
        with patch("teatree.core.speak.speak", side_effect=RuntimeError("audio boom")):
            sent = notify_user(
                "tests are green",
                kind=NotifyKind.INFO,
                idempotency_key="speak-raises",
                backend=_backend(),
                user_id="U_ME",
            )
        assert sent is True
