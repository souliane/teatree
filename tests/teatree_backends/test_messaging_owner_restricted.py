"""``OwnerRestrictedMessaging`` — the dm_only hard-constraint wrapper.

The wrapper is the runtime half of the ``dm_only`` scope profile: a bot that may
reach ONLY its owner's own DM. These tests pin the security contract — every
non-owner destination raises, owner-DM traffic passes, and non-destination calls
plus unnamed inner attributes delegate to the wrapped backend.
"""

import pytest

from teatree.backends.messaging_owner_restricted import OwnerDmOnlyError, OwnerRestrictedMessaging


class _RecordingBackend:
    """A minimal MessagingBackend double that echoes its target back."""

    name = "SlackBotBackend"

    def __init__(self) -> None:
        self.voice_mode = ""

    def post_message(self, *, channel: str, text: str, thread_ts: str = "", blocks: object = None) -> dict[str, object]:
        return {"ok": True, "channel": channel, "text": text}

    def post_reply(self, *, channel: str, ts: str, text: str) -> dict[str, object]:
        return {"ok": True, "channel": channel}

    def react(self, *, channel: str, ts: str, emoji: str) -> dict[str, object]:
        return {"ok": True, "channel": channel}

    def post_routed(self, *, channel: str, text: str, thread_ts: str = "") -> dict[str, object]:
        return {"ok": True, "channel": channel}

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> dict[str, object]:
        return {"ok": True, "channel": channel}

    def post_audio_dm(
        self, *, channel: str, filepath: str, text: str, thread_ts: str = "", title: str = ""
    ) -> dict[str, object]:
        return {"ok": True, "channel": channel}

    def fetch_message(self, *, channel: str, ts: str) -> dict[str, object]:
        return {"channel": channel}

    def fetch_thread_replies(self, *, channel: str, thread_ts: str) -> list[dict[str, object]]:
        return [{"channel": channel}]

    def fetch_channel_history(self, *, channel: str, limit: int = 50) -> list[dict[str, object]]:
        return [{"channel": channel}]

    def open_dm(self, user_id: str) -> str:
        return f"D-{user_id}"

    def fetch_dms(self, *, since: str = "") -> list[dict[str, object]]:
        return [{"since": since}]

    def fetch_mentions(self, *, since: str = "") -> list[dict[str, object]]:
        return []

    def fetch_reactions(self, *, since: str = "") -> list[dict[str, object]]:
        return []

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return f"https://slack/{channel}/{ts}"

    def resolve_user_id(self, handle: str) -> str:
        return "U-owner"

    def auth_test(self) -> dict[str, object]:
        return {"ok": True, "user_id": "U-owner"}

    def set_voice_classifier_mode(self, mode: str) -> str:
        self.voice_mode = mode
        return mode


def _wrap(**kwargs: str) -> tuple[OwnerRestrictedMessaging, _RecordingBackend]:
    inner = _RecordingBackend()
    defaults = {"dm_channel_id": "D123", "user_id": "U-owner"}
    defaults.update(kwargs)
    return OwnerRestrictedMessaging(inner, **defaults), inner


_WRITE_CALLS = {
    "post_message": lambda w, ch: w.post_message(channel=ch, text="hi"),
    "post_reply": lambda w, ch: w.post_reply(channel=ch, ts="1", text="hi"),
    "react": lambda w, ch: w.react(channel=ch, ts="1", emoji="wave"),
    "post_routed": lambda w, ch: w.post_routed(channel=ch, text="hi"),
    "react_routed": lambda w, ch: w.react_routed(channel=ch, ts="1", emoji="wave"),
    "post_audio_dm": lambda w, ch: w.post_audio_dm(channel=ch, filepath="/a.mp3", text="hi"),
    "fetch_message": lambda w, ch: w.fetch_message(channel=ch, ts="1"),
    "fetch_thread_replies": lambda w, ch: w.fetch_thread_replies(channel=ch, thread_ts="1"),
    "fetch_channel_history": lambda w, ch: w.fetch_channel_history(channel=ch),
}


class TestOwnerDmPasses:
    @pytest.mark.parametrize("call", _WRITE_CALLS.values(), ids=list(_WRITE_CALLS))
    def test_provisioned_dm_channel_is_allowed(self, call: object) -> None:
        wrapper, _ = _wrap()
        assert call(wrapper, "D123") is not None  # type: ignore[operator]

    @pytest.mark.parametrize("call", _WRITE_CALLS.values(), ids=list(_WRITE_CALLS))
    def test_owner_user_id_is_allowed(self, call: object) -> None:
        # Before an ``open_dm`` resolves the IM, Slack accepts the user's ``U…`` id.
        wrapper, _ = _wrap()
        assert call(wrapper, "U-owner") is not None  # type: ignore[operator]


class TestNonOwnerRefused:
    @pytest.mark.parametrize("call", _WRITE_CALLS.values(), ids=list(_WRITE_CALLS))
    @pytest.mark.parametrize("channel", ["C-public", "D-colleague", "G-private", "U-someone-else"])
    def test_non_owner_destination_raises(self, call: object, channel: str) -> None:
        wrapper, _ = _wrap()
        with pytest.raises(OwnerDmOnlyError):
            call(wrapper, channel)  # type: ignore[operator]

    def test_open_dm_owner_allowed(self) -> None:
        wrapper, _ = _wrap()
        assert wrapper.open_dm("U-owner") == "D-U-owner"

    def test_open_dm_colleague_refused(self) -> None:
        wrapper, _ = _wrap()
        with pytest.raises(OwnerDmOnlyError):
            wrapper.open_dm("U-colleague")


class TestFailClosedIdentity:
    def test_no_owner_identity_refuses_everything(self) -> None:
        # A restricted bot with neither dm_channel_id nor user_id knows no owner —
        # it must refuse all guarded traffic rather than fall open.
        wrapper, _ = _wrap(dm_channel_id="", user_id="")
        with pytest.raises(OwnerDmOnlyError):
            wrapper.post_message(channel="D123", text="hi")
        with pytest.raises(OwnerDmOnlyError):
            wrapper.open_dm("U-owner")


class TestPassThroughAndDelegation:
    def test_auth_test_passes_through(self) -> None:
        wrapper, _ = _wrap()
        assert wrapper.auth_test() == {"ok": True, "user_id": "U-owner"}

    def test_owner_scoped_reads_pass_through(self) -> None:
        wrapper, _ = _wrap()
        assert wrapper.fetch_dms(since="0") == [{"since": "0"}]
        assert wrapper.fetch_mentions() == []
        assert wrapper.resolve_user_id("owner") == "U-owner"
        assert wrapper.get_permalink(channel="C-any", ts="1").startswith("https://slack/")

    def test_unnamed_inner_attributes_delegate(self) -> None:
        wrapper, inner = _wrap()
        # ``set_voice_classifier_mode`` is applied post-construction by the backend
        # factory — it must reach the inner backend through the wrapper.
        assert wrapper.set_voice_classifier_mode("WARN") == "WARN"
        assert inner.voice_mode == "WARN"
        assert wrapper.name == "SlackBotBackend"
