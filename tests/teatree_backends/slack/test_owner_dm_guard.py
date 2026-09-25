"""The dm_only owner-restriction guard — ``assert_owner_dm`` + ``SlackBotBackend`` enforcement.

A bot on the dm_only scope profile (``owner_dm_only=True``) may reach ONLY its
owner's own DM. The token funnels ``_channel_token`` / ``_route_token`` refuse a
non-owner destination, and the ``_post`` chokepoint refuses a non-owner
``chat.postMessage`` / ``conversations.open`` and every ``conversations.join`` /
``invite`` / ``create`` — all before any HTTP call. These tests pin that contract.
"""

from unittest.mock import patch

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


class TestPostChokepointGuard:
    @pytest.mark.parametrize("method", ["conversations.join", "conversations.invite", "conversations.create"])
    def test_channel_reach_method_refused_outright(self, method: str) -> None:
        bot = _dm_only_bot()
        with patch.object(bot._http, "post") as transport, pytest.raises(OwnerDmOnlyError, match=method):
            bot._post(method, {"channel": _OWNER_DM, "users": _OWNER_UID, "name": "owner"})
        transport.assert_not_called()

    def test_join_conversation_refused(self) -> None:
        bot = _dm_only_bot()
        with patch.object(bot._http, "post") as transport, pytest.raises(OwnerDmOnlyError):
            bot.join_conversation("C-public")
        transport.assert_not_called()

    @pytest.mark.parametrize("channel", ["U-someone-else", "D-colleague", "C-public", ""])
    def test_post_message_to_non_owner_refused(self, channel: str) -> None:
        bot = _dm_only_bot()
        with patch.object(bot._http, "post") as transport, pytest.raises(OwnerDmOnlyError):
            bot._post("chat.postMessage", {"channel": channel, "text": "leak"}, idempotent=False)
        transport.assert_not_called()

    @pytest.mark.parametrize("users", ["U-someone-else", f"{_OWNER_UID},U-someone-else", _OWNER_DM, ""])
    def test_open_dm_to_non_owner_refused(self, users: str) -> None:
        bot = _dm_only_bot()
        with patch.object(bot._http, "post") as transport, pytest.raises(OwnerDmOnlyError):
            bot.open_dm(users)
        transport.assert_not_called()

    def test_owner_dm_post_reaches_transport(self) -> None:
        bot = _dm_only_bot()
        with patch.object(bot._http, "post", return_value={"ok": True, "ts": "1.0"}) as transport:
            assert bot.post_message(channel=_OWNER_DM, text="hi")["ok"]
        assert transport.call_args.args == ("chat.postMessage",)
        assert transport.call_args.kwargs["json"]["channel"] == _OWNER_DM

    def test_owner_routed_post_by_user_id_reaches_transport(self) -> None:
        bot = _dm_only_bot()
        with patch.object(bot._http, "post", return_value={"ok": True, "ts": "1.0"}) as transport:
            assert bot.post_routed(channel=_OWNER_UID, text="hi")["ok"]
        assert transport.call_args.kwargs["json"]["channel"] == _OWNER_UID

    def test_owner_open_dm_reaches_transport(self) -> None:
        bot = SlackBotBackend(bot_token="xoxb-test", user_id=_OWNER_UID, owner_dm_only=True)
        with patch.object(bot._http, "post", return_value={"ok": True, "channel": {"id": _OWNER_DM}}) as transport:
            assert bot.open_dm(_OWNER_UID) == _OWNER_DM
        transport.assert_called_once_with(
            "conversations.open", token="xoxb-test", json={"users": _OWNER_UID}, idempotent=True
        )

    def test_full_profile_bot_is_unrestricted(self) -> None:
        full = SlackBotBackend(bot_token="xoxb-test", user_id=_OWNER_UID, dm_channel_id=_OWNER_DM)
        with patch.object(full._http, "post", return_value={"ok": True}) as transport:
            full.join_conversation("C-public")
            full.open_dm("U-someone-else")
            full._post("chat.postMessage", {"channel": "C-public", "text": "hi"}, idempotent=False)
        assert [call.args[0] for call in transport.call_args_list] == [
            "conversations.join",
            "conversations.open",
            "chat.postMessage",
        ]
