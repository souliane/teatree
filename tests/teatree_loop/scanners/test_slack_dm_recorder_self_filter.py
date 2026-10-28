"""The recorder drops the bot's own outbound DMs (#1346) and on-behalf posts (#1941).

Slack delivers the bot's own posts as plain ``message`` events whose ``user`` or
``bot_id`` is the bot's, and an on-behalf post sent with the human's own token carries
the human's ``user`` id plus an ``api_app_id``. Neither may reach the answer cycle.
When the bot's identity cannot be resolved the recorder fails closed: nothing is recorded.
"""

from dataclasses import dataclass, field

from django.test import TestCase

from teatree.core.models import PendingChatInjection
from teatree.loop.scanners.slack_dm_recorder import SlackDmRecorder
from teatree.types import RawAPIDict

_OWN_USER_ID = "U_BOT_SELF"
_OWN_BOT_ID = "B_BOT_SELF"
_USER_ID = "U_HUMAN"
_CHANNEL = "D0DEMOCLNT1"


@dataclass
class FakeMessagingWithAuth:
    auth_response: RawAPIDict = field(
        default_factory=lambda: {"ok": True, "user_id": _OWN_USER_ID, "bot_id": _OWN_BOT_ID},
    )
    auth_test_calls: int = 0
    user_id: str = _USER_ID

    def auth_test(self) -> RawAPIDict:
        self.auth_test_calls += 1
        return self.auth_response


class _TransportDownError(RuntimeError):
    pass


@dataclass
class RaisingAuthBackend(FakeMessagingWithAuth):
    def auth_test(self) -> RawAPIDict:
        raise _TransportDownError


def _record(events: list[RawAPIDict], backend: FakeMessagingWithAuth | None = None) -> int:
    return SlackDmRecorder(backend=backend or FakeMessagingWithAuth(), overlay="demo").record_events(events)


def _recorded_texts() -> list[str]:
    return list(PendingChatInjection.objects.order_by("slack_ts").values_list("text", flat=True))


class TestSelfFilter(TestCase):
    def test_drops_bot_outbound_when_user_matches_own_user_id(self) -> None:
        recorded = _record(
            [
                {
                    "ts": "1779823899.073799",
                    "user": _OWN_USER_ID,
                    "channel": _CHANNEL,
                    "text": "teatree#1283 already fixed on main",
                },
                {"ts": "1779823900.000000", "user": _USER_ID, "channel": _CHANNEL, "text": "actual user reply"},
            ]
        )

        assert recorded == 1
        row = PendingChatInjection.objects.get()
        assert row.text == "actual user reply"
        assert row.user_id == _USER_ID
        assert row.slack_ts == "1779823900.000000"

    def test_drops_bot_outbound_when_bot_id_matches_own_bot_id(self) -> None:
        """Bot-style messages may carry only ``bot_id`` (no ``user``)."""
        _record(
            [
                {"ts": "1.0", "bot_id": _OWN_BOT_ID, "channel": _CHANNEL, "text": "bot post (no user field)"},
                {"ts": "2.0", "user": _USER_ID, "channel": _CHANNEL, "text": "real user message"},
            ]
        )

        assert _recorded_texts() == ["real user message"]

    def test_user_messages_still_recorded(self) -> None:
        recorded = _record(
            [
                {"ts": "1.0", "user": _USER_ID, "channel": _CHANNEL, "text": "hi"},
                {"ts": "2.0", "user": _USER_ID, "channel": _CHANNEL, "text": "more"},
            ]
        )

        assert recorded == 2
        assert PendingChatInjection.objects.count() == 2

    def test_fail_closed_when_auth_test_returns_not_ok(self) -> None:
        backend = FakeMessagingWithAuth(auth_response={"ok": False, "error": "invalid_auth"})

        recorded = _record([{"ts": "1.0", "user": _USER_ID, "channel": _CHANNEL, "text": "real user message"}], backend)

        assert recorded == 0
        assert PendingChatInjection.objects.count() == 0

    def test_fail_closed_when_auth_test_returns_empty(self) -> None:
        backend = FakeMessagingWithAuth(auth_response={})

        recorded = _record([{"ts": "1.0", "user": _USER_ID, "channel": _CHANNEL, "text": "user msg"}], backend)

        assert recorded == 0
        assert PendingChatInjection.objects.count() == 0

    def test_fail_closed_when_auth_test_raises(self) -> None:
        recorded = _record(
            [{"ts": "1.0", "user": _USER_ID, "channel": _CHANNEL, "text": "user msg"}], RaisingAuthBackend()
        )

        assert recorded == 0
        assert PendingChatInjection.objects.count() == 0

    def test_auth_test_resolved_once_per_recorder(self) -> None:
        backend = FakeMessagingWithAuth()
        recorder = SlackDmRecorder(backend=backend, overlay="demo")

        for ts in ("1.0", "2.0", "3.0"):
            recorder.record_events([{"ts": ts, "user": _USER_ID, "channel": _CHANNEL, "text": "msg"}])

        assert backend.auth_test_calls == 1
        assert PendingChatInjection.objects.count() == 3


class TestOnBehalfFilter(TestCase):
    """``api_app_id`` separates an on-behalf post from a DM the human typed (#1941).

    Every row surviving the identity filter carries the human's own ``user`` id, so
    the author id alone cannot tell the two apart (the #2907 regression).
    """

    def test_on_behalf_post_is_not_recorded(self) -> None:
        recorded = _record(
            [
                {
                    "ts": "1780653892.068199",
                    "user": _USER_ID,
                    "api_app_id": "A0DEMOAPP1",
                    "channel": _CHANNEL,
                    "text": "PR merged — evidence at https://example/pr/1",
                },
            ]
        )

        assert recorded == 0
        assert PendingChatInjection.objects.count() == 0

    def test_genuine_human_dm_is_recorded(self) -> None:
        recorded = _record(
            [{"ts": "1780653900.000000", "user": _USER_ID, "channel": _CHANNEL, "text": "why did the pipeline fail?"}]
        )

        assert recorded == 1
        row = PendingChatInjection.objects.get()
        assert row.text == "why did the pipeline fail?"
        assert row.slack_ts == "1780653900.000000"

    def test_both_shapes_in_one_batch(self) -> None:
        recorded = _record(
            [
                {
                    "ts": "1.0",
                    "user": _USER_ID,
                    "api_app_id": "A0DEMOAPP1",
                    "channel": _CHANNEL,
                    "text": "on-behalf answer post",
                },
                {"ts": "2.0", "user": _USER_ID, "channel": _CHANNEL, "text": "how do I resolve this?"},
            ]
        )

        assert recorded == 1
        assert _recorded_texts() == ["how do I resolve this?"]
