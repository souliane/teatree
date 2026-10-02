"""Skip re-ingesting the bot's OWN TTS audio attachment (teatree#2089).

The Slack-TTS feature (#2050/#2060) attaches a synthesised ``speech.m4a`` to
the bot's own DM messages — the audio is a spoken copy of text the bot already
wrote in the same message. When the loop later reads Slack history, surfacing
that audio attachment to the agent makes it download/transcribe a redundant
copy: pure token waste for zero new information.

The fix strips the bot's own TTS audio attachment at the single Slack-read
chokepoint, reusing the existing bot-identity check
(:func:`is_self_authored`). Audio on messages authored by OTHERS (the user's
voice notes) must still flow through — the skip must not be over-broad.
"""

from typing import cast

from teatree.loop.scanners.slack_self_filter import OwnSlackIdentity, is_tts_audio_file, strip_self_audio_attachments
from teatree.types import RawAPIDict

_OWN_USER_ID = "U_BOT_SELF"
_OWN_BOT_ID = "B_BOT_SELF"
_USER_ID = "U_HUMAN"

_IDENTITY = OwnSlackIdentity(user_id=_OWN_USER_ID, bot_id=_OWN_BOT_ID)


def _files(message: RawAPIDict) -> list[RawAPIDict]:
    return cast("list[RawAPIDict]", message.get("files", []))


def _tts_file() -> RawAPIDict:
    return {
        "id": "F0SPEECH",
        "name": "speech.m4a",
        "mimetype": "audio/mp4",
        "filetype": "m4a",
        "url_private": "https://files.slack.com/files-pri/T/F0SPEECH/speech.m4a",
    }


def _user_voice_note() -> RawAPIDict:
    return {
        "id": "F0VOICE",
        "name": "audio_message.m4a",
        "mimetype": "audio/mp4",
        "filetype": "m4a",
        "url_private": "https://files.slack.com/files-pri/T/F0VOICE/audio_message.m4a",
    }


def _image_file() -> RawAPIDict:
    return {
        "id": "F0IMG",
        "name": "screenshot.png",
        "mimetype": "image/png",
        "filetype": "png",
        "url_private": "https://files.slack.com/files-pri/T/F0IMG/screenshot.png",
    }


class TestIsTtsAudioFile:
    def test_classifies_audio_mimetype_as_audio(self) -> None:
        assert is_tts_audio_file(_tts_file()) is True

    def test_classifies_audio_by_filetype_when_mimetype_absent(self) -> None:
        assert is_tts_audio_file({"name": "speech.m4a", "filetype": "m4a"}) is True

    def test_image_attachment_is_not_audio(self) -> None:
        assert is_tts_audio_file(_image_file()) is False

    def test_non_dict_entry_is_not_audio(self) -> None:
        assert is_tts_audio_file("not-a-dict") is False

    def test_entry_without_audio_markers_is_not_audio(self) -> None:
        assert is_tts_audio_file({"name": "notes.txt", "filetype": "text"}) is False


class TestStripSelfAudioAttachments:
    def test_strips_bot_own_audio_attachment_keeps_text(self) -> None:
        bot_msg: RawAPIDict = {
            "ts": "1.0",
            "user": _OWN_USER_ID,
            "text": "PR #2089 merged, shipping now",
            "files": [_tts_file()],
        }

        [out] = strip_self_audio_attachments([bot_msg], _IDENTITY)

        assert out["text"] == "PR #2089 merged, shipping now"
        assert out.get("files", []) == []

    def test_strips_bot_own_audio_when_only_bot_id_present(self) -> None:
        bot_msg: RawAPIDict = {
            "ts": "1.0",
            "bot_id": _OWN_BOT_ID,
            "text": "done",
            "files": [_tts_file()],
        }

        [out] = strip_self_audio_attachments([bot_msg], _IDENTITY)

        assert out.get("files", []) == []

    def test_keeps_user_authored_voice_note(self) -> None:
        user_msg: RawAPIDict = {
            "ts": "2.0",
            "user": _USER_ID,
            "text": "",
            "files": [_user_voice_note()],
        }

        [out] = strip_self_audio_attachments([user_msg], _IDENTITY)

        assert len(_files(out)) == 1
        assert _files(out)[0]["id"] == "F0VOICE"

    def test_keeps_bot_non_audio_attachment(self) -> None:
        bot_msg: RawAPIDict = {
            "ts": "1.0",
            "user": _OWN_USER_ID,
            "text": "evidence attached",
            "files": [_image_file()],
        }

        [out] = strip_self_audio_attachments([bot_msg], _IDENTITY)

        assert len(_files(out)) == 1
        assert _files(out)[0]["id"] == "F0IMG"

    def test_strips_only_audio_from_mixed_bot_attachments(self) -> None:
        bot_msg: RawAPIDict = {
            "ts": "1.0",
            "user": _OWN_USER_ID,
            "text": "report + spoken copy",
            "files": [_tts_file(), _image_file()],
        }

        [out] = strip_self_audio_attachments([bot_msg], _IDENTITY)

        ids = [f["id"] for f in _files(out)]
        assert ids == ["F0IMG"]

    def test_message_without_files_unchanged(self) -> None:
        bot_msg: RawAPIDict = {"ts": "1.0", "user": _OWN_USER_ID, "text": "no attachment"}

        [out] = strip_self_audio_attachments([bot_msg], _IDENTITY)

        assert out == {"ts": "1.0", "user": _OWN_USER_ID, "text": "no attachment"}

    def test_fail_open_when_identity_none(self) -> None:
        bot_msg: RawAPIDict = {
            "ts": "1.0",
            "user": _OWN_USER_ID,
            "text": "t",
            "files": [_tts_file()],
        }

        [out] = strip_self_audio_attachments([bot_msg], None)

        assert len(_files(out)) == 1

    def test_idempotent_on_already_stripped_message(self) -> None:
        bot_msg: RawAPIDict = {
            "ts": "1.0",
            "user": _OWN_USER_ID,
            "text": "x",
            "files": [_tts_file()],
        }

        once = strip_self_audio_attachments([bot_msg], _IDENTITY)
        twice = strip_self_audio_attachments(once, _IDENTITY)

        assert twice[0].get("files", []) == []

    def test_preserves_other_messages_in_batch(self) -> None:
        batch: list[RawAPIDict] = [
            {"ts": "1.0", "user": _OWN_USER_ID, "text": "bot", "files": [_tts_file()]},
            {"ts": "2.0", "user": _USER_ID, "text": "user voice", "files": [_user_voice_note()]},
        ]

        out = strip_self_audio_attachments(batch, _IDENTITY)

        assert out[0].get("files", []) == []
        assert len(_files(out[1])) == 1
