"""``notify_user`` delivers the bot→user DM through the shared speak chokepoint (#2060).

The IM/DM arm of text-to-speech: a successful bot→user DM goes through
:func:`teatree.core.speak.deliver_user_dm`, which posts ONE message
carrying the text + attached audio when ``slack`` is on (and reads it
locally when ``local`` plays DMs), degrading to a text-only post otherwise.
Speaking/attaching must never break or block the notification path. Only
the messaging backend (network boundary) and the synthesis seam are mocked.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.notify import NotifyKind, notify_user
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
                audience=NotifyAudience.OWNER_DELIVERY,
                backend=backend,
                user_id="U_ME",
            )
        assert sent is True
        backend.post_message.assert_called_once()
        backend.post_audio_dm.assert_not_called()

    def test_delivers_via_chat_post_message_when_slack_on(self) -> None:
        """Regression for #2054: notify_user must deliver via chat.postMessage.

        Pre-fix: with speak.slack=True the text body was sent via
        post_audio_dm (files.getUploadURLExternal +
        files.completeUploadExternal). That response carries no ``ts`` at
        the top level, so _deliver_dm logged "Slack post returned no message
        ts" and returned False — the DM was never delivered.

        Fix: _deliver_dm always uses backend.post_message (chat.postMessage)
        for the canonical text delivery whose response reliably carries
        ``ok:true`` + ``ts``. The audio side-effect (post_audio_dm) may
        still run on a background path, but the ts used for audit and
        idempotency comes from post_message, not from the file-upload
        response.
        """
        backend = _backend()
        # Simulate the pre-fix failure: post_audio_dm returns a body with no ts
        # at the top level (the exact shape Slack's completeUploadExternal gives).
        backend.post_audio_dm.return_value = {"ok": True, "files": [{"id": "F1"}]}
        with (
            patch("teatree.core.speak.resolve_speak", return_value=SpeakConfig(slack=True)),
            patch("teatree.core.speak.synthesise", return_value=__import__("pathlib").Path("/tmp/x/speech.m4a")),
            patch("teatree.core.speak.shutil.rmtree"),
        ):
            sent = notify_user(
                "tests are green",
                kind=NotifyKind.INFO,
                idempotency_key="notify-audio-ts-regression",
                audience=NotifyAudience.OWNER_DELIVERY,
                backend=backend,
                user_id="U_ME",
            )
        # Must be True: the DM must land even when audio body has no ts.
        assert sent is True
        # The text delivery goes via chat.postMessage (reliable ts source).
        backend.post_message.assert_called_once()

    def test_attaches_audio_to_the_dm_when_slack_on(self) -> None:
        backend = _backend()
        with (
            patch("teatree.core.speak.resolve_speak", return_value=SpeakConfig(slack=True)),
            patch("teatree.core.speak.synthesise", return_value=__import__("pathlib").Path("/tmp/x/speech.m4a")),
            patch("teatree.core.speak.shutil.rmtree"),
        ):
            sent = notify_user(
                "tests are green",
                kind=NotifyKind.INFO,
                idempotency_key="speak-audio",
                audience=NotifyAudience.OWNER_DELIVERY,
                backend=backend,
                user_id="U_ME",
            )
        assert sent is True
        # After the fix, the audio attachment still runs as a side-effect.
        backend.post_audio_dm.assert_called_once()
        # The text delivery also always uses post_message (the reliable ts source).
        backend.post_message.assert_called_once()

    def test_does_not_speak_when_delivery_fails(self) -> None:
        backend = _backend()
        backend.post_message.return_value = {"ok": False, "error": "channel_not_found"}
        with patch("teatree.core.speak.resolve_speak", return_value=SpeakConfig()):
            sent = notify_user(
                "tests are green",
                kind=NotifyKind.INFO,
                idempotency_key="speak-on-failure",
                audience=NotifyAudience.OWNER_DELIVERY,
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
                audience=NotifyAudience.OWNER_DELIVERY,
                backend=backend,
                user_id="U_ME",
            )
        assert sent is True
        backend.post_message.assert_called_once()

    def test_audio_threads_under_delivered_ts_without_repeating_text(self) -> None:
        """F4.4: the text lands exactly once; audio threads under it with no repeat.

        Pre-fix the sidecar posted the audio DM with the SAME text as its
        ``initial_comment``, so with ``slack`` on the body was delivered twice
        (``post_message`` body + the audio DM's identical comment). The fix
        threads the audio under the delivered ``ts`` (``thread_ts=posted_ts``)
        with an EMPTY ``initial_comment`` — text delivered once via
        ``post_message``, audio a threaded enhancement.
        """
        backend = _backend()
        with (
            patch("teatree.core.speak.resolve_speak", return_value=SpeakConfig(slack=True)),
            patch("teatree.core.speak.synthesise", return_value=__import__("pathlib").Path("/tmp/x/speech.m4a")),
            patch("teatree.core.speak.shutil.rmtree"),
        ):
            sent = notify_user(
                "tests are green",
                kind=NotifyKind.INFO,
                idempotency_key="notify-single-delivery",
                audience=NotifyAudience.OWNER_DELIVERY,
                backend=backend,
                user_id="U_ME",
            )
        assert sent is True
        # The canonical text delivery goes out exactly ONCE.
        backend.post_message.assert_called_once()
        # The audio attaches exactly once, threaded under the delivered message
        # (``thread_ts`` == the ``post_message`` ts) with NO repeated text.
        backend.post_audio_dm.assert_called_once()
        audio_kwargs = backend.post_audio_dm.call_args.kwargs
        assert audio_kwargs["thread_ts"] == "1700000000.000000"
        assert audio_kwargs["text"] == "", "audio DM must not repeat the text body (empty initial_comment)"

    def test_no_audio_when_delivery_reports_not_ok(self) -> None:
        """F4.4: the sidecar runs ONLY after the ok/ts check succeeds.

        Pre-fix the sidecar fired before the ``ok:false`` check, so a failed
        ``post_message`` still attached audio to a DM that never landed — and
        the FAILED finalize then drove a retry that re-attached, tripling it.
        With ``slack`` on and an ``ok:false`` post, no audio must be attached.
        """
        backend = _backend()
        backend.post_message.return_value = {"ok": False, "error": "channel_not_found"}
        with (
            patch("teatree.core.speak.resolve_speak", return_value=SpeakConfig(slack=True)),
            patch("teatree.core.speak.synthesise", return_value=__import__("pathlib").Path("/tmp/x/speech.m4a")),
            patch("teatree.core.speak.shutil.rmtree"),
        ):
            sent = notify_user(
                "tests are green",
                kind=NotifyKind.INFO,
                idempotency_key="notify-no-audio-on-failure",
                audience=NotifyAudience.OWNER_DELIVERY,
                backend=backend,
                user_id="U_ME",
            )
        assert sent is False
        backend.post_audio_dm.assert_not_called()

    def test_no_audio_when_post_returns_ok_but_no_ts(self) -> None:
        """F4.4: an ``ok:true`` with no ``ts`` is a non-delivery — no audio sidecar."""
        backend = _backend()
        backend.post_message.return_value = {"ok": True}
        with (
            patch("teatree.core.speak.resolve_speak", return_value=SpeakConfig(slack=True)),
            patch("teatree.core.speak.synthesise", return_value=__import__("pathlib").Path("/tmp/x/speech.m4a")),
            patch("teatree.core.speak.shutil.rmtree"),
        ):
            sent = notify_user(
                "tests are green",
                kind=NotifyKind.INFO,
                idempotency_key="notify-no-audio-no-ts",
                audience=NotifyAudience.OWNER_DELIVERY,
                backend=backend,
                user_id="U_ME",
            )
        assert sent is False
        backend.post_audio_dm.assert_not_called()
