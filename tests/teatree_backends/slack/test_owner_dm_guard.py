"""The dm_only owner-restriction guard — ``assert_owner_dm`` + ``SlackBotBackend`` enforcement.

A bot on the dm_only scope profile (``owner_dm_only=True``) may reach ONLY its
owner's own DM. Enforcement lives at the two token funnels ``_channel_token`` /
``_route_token``, so every write primitive (post/react/routed/audio) refuses a
non-owner destination before any HTTP call. These tests pin that contract.
"""

import pytest

from teatree.backends.slack.bot import SlackBotBackend
from teatree.backends.slack.routing import OwnerDmOnlyError, assert_owner_dm
from teatree.backends.slack.token_policy import SlackOp

_OWNER_DM = "D-owner"
_OWNER_UID = "U-owner"


def _dm_only_bot() -> SlackBotBackend:
    return SlackBotBackend(bot_token="xoxb-test", user_id=_OWNER_UID, dm_channel_id=_OWNER_DM, owner_dm_only=True)


class TestAssertOwnerDm:
    def test_no_op_when_not_owner_restricted(self) -> None:
        # A full-profile bot never raises, whatever the destination.
        assert_owner_dm("C-any", owner_dm_only=False, dm_channel_id=_OWNER_DM, user_id=_OWNER_UID)

    @pytest.mark.parametrize("channel", [_OWNER_DM, _OWNER_UID])
    def test_owner_destination_allowed(self, channel: str) -> None:
        assert_owner_dm(channel, owner_dm_only=True, dm_channel_id=_OWNER_DM, user_id=_OWNER_UID)

    @pytest.mark.parametrize("channel", ["C-public", "D-colleague", "G-private", "U-someone-else"])
    def test_non_owner_destination_refused(self, channel: str) -> None:
        with pytest.raises(OwnerDmOnlyError):
            assert_owner_dm(channel, owner_dm_only=True, dm_channel_id=_OWNER_DM, user_id=_OWNER_UID)

    def test_fail_closed_without_owner_identity(self) -> None:
        # No dm_channel_id and no user_id ⇒ every destination is refused, not fall-open.
        with pytest.raises(OwnerDmOnlyError):
            assert_owner_dm(_OWNER_DM, owner_dm_only=True, dm_channel_id="", user_id="")


class TestBackendFunnelGuards:
    def test_channel_token_refuses_non_owner(self) -> None:
        bot = _dm_only_bot()
        with pytest.raises(OwnerDmOnlyError):
            bot._channel_token("C-public", op=SlackOp.WRITE)

    def test_channel_token_allows_owner_dm(self) -> None:
        bot = _dm_only_bot()
        assert bot._channel_token(_OWNER_DM, op=SlackOp.WRITE)  # a token, no raise

    def test_route_token_refuses_non_owner(self) -> None:
        bot = _dm_only_bot()
        with pytest.raises(OwnerDmOnlyError):
            bot._route_token("C-public")

    def test_route_token_allows_owner_dm(self) -> None:
        bot = _dm_only_bot()
        assert bot._route_token(_OWNER_DM)

    def test_full_bot_funnels_never_raise(self) -> None:
        full = SlackBotBackend(bot_token="xoxb-test", user_id=_OWNER_UID, dm_channel_id=_OWNER_DM)
        assert full._channel_token("C-public", op=SlackOp.WRITE)
        assert full._route_token("C-public")


class TestWritePrimitivesRefuseNonOwner:
    """Each public write refuses a non-owner destination before any HTTP call."""

    def test_post_message_refused(self) -> None:
        with pytest.raises(OwnerDmOnlyError):
            _dm_only_bot().post_message(channel="C-public", text="leak")

    def test_post_reply_refused(self) -> None:
        with pytest.raises(OwnerDmOnlyError):
            _dm_only_bot().post_reply(channel="C-public", ts="1", text="leak")

    def test_react_refused(self) -> None:
        with pytest.raises(OwnerDmOnlyError):
            _dm_only_bot().react(channel="C-public", ts="1", emoji="wave")

    def test_post_routed_refused(self) -> None:
        with pytest.raises(OwnerDmOnlyError):
            _dm_only_bot().post_routed(channel="C-public", text="leak")

    def test_react_routed_refused(self) -> None:
        with pytest.raises(OwnerDmOnlyError):
            _dm_only_bot().react_routed(channel="C-public", ts="1", emoji="wave")

    def test_post_audio_dm_refused(self) -> None:
        with pytest.raises(OwnerDmOnlyError):
            _dm_only_bot().post_audio_dm(channel="C-public", filepath="/a.mp3", text="leak")
