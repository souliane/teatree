"""``SlackDmRecorder.record_events`` persists each new bot-DM user message once (#1014)."""

from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.test import TestCase

from teatree.core.models import OperatorIdentity, PendingChatInjection
from teatree.loop.scanners.slack_dm_recorder import SlackDmRecorder
from teatree.types import RawAPIDict

_OWNER = "U0DEMOUSER1"


@dataclass
class FakeMessaging:
    identity_resolves: bool = True
    user_id: str = _OWNER

    def auth_test(self) -> RawAPIDict:
        if not self.identity_resolves:
            return {"ok": False}
        return {"ok": True, "user_id": "U_BOT_SELF", "bot_id": "B_BOT_SELF"}


def _recorder(overlay: str = "demo", *, identity_resolves: bool = True, owner: str = _OWNER) -> SlackDmRecorder:
    return SlackDmRecorder(backend=FakeMessaging(identity_resolves=identity_resolves, user_id=owner), overlay=overlay)


class TestRecordEvents(TestCase):
    def test_one_user_message_creates_one_row(self) -> None:
        recorded = _recorder().record_events(
            [{"ts": "1700000000.0001", "user": "U0DEMOUSER1", "channel": "D0DEMOTEAM1", "text": "Hello, agent"}]
        )

        assert recorded == 1
        row = PendingChatInjection.objects.get()
        assert row.text == "Hello, agent"
        assert row.slack_ts == "1700000000.0001"
        assert row.user_id == "U0DEMOUSER1"
        assert row.channel == "D0DEMOTEAM1"
        assert row.overlay == "demo"
        assert row.loop_replied_at is None

    def test_no_events_records_nothing(self) -> None:
        assert _recorder().record_events([]) == 0
        assert PendingChatInjection.objects.count() == 0

    def test_same_ts_recorded_twice_creates_one_row(self) -> None:
        recorder = _recorder()
        event: RawAPIDict = {"ts": "1.0", "user": "U0DEMOUSER1", "channel": "D0DEMOTEAM1", "text": "First"}

        first = recorder.record_events([event])
        second = recorder.record_events([event])

        assert (first, second) == (1, 0)
        assert PendingChatInjection.objects.count() == 1

    def test_event_ts_fallback_when_ts_absent(self) -> None:
        recorded = _recorder().record_events(
            [{"event_ts": "5.0", "user": "U0DEMOUSER1", "channel": "D0DEMOTEAM1", "text": "no ts"}]
        )

        assert recorded == 1
        assert PendingChatInjection.objects.filter(slack_ts="5.0").count() == 1

    def test_messages_without_ts_or_text_are_skipped(self) -> None:
        recorded = _recorder().record_events(
            [
                {"user": "U0DEMOUSER1", "channel": "D", "text": "no-ts"},
                {"ts": "1.0", "user": "U0DEMOUSER1", "channel": "D", "text": ""},
                {"ts": "2.0", "user": "U0DEMOUSER1", "channel": "D", "text": "good"},
            ]
        )

        assert recorded == 1
        assert list(PendingChatInjection.objects.values_list("text", flat=True)) == ["good"]

    def test_multiple_user_messages_create_multiple_rows(self) -> None:
        recorded = _recorder().record_events(
            [
                {"ts": "1.0", "user": "U0DEMOUSER1", "channel": "D", "text": "first"},
                {"ts": "2.0", "user": "U0DEMOUSER1", "channel": "D", "text": "second"},
            ]
        )

        assert recorded == 2
        rows = list(PendingChatInjection.objects.order_by("slack_ts"))
        assert [row.slack_ts for row in rows] == ["1.0", "2.0"]
        assert [row.text for row in rows] == ["first", "second"]

    def test_default_overlay_is_empty_string(self) -> None:
        SlackDmRecorder(backend=FakeMessaging()).record_events(
            [{"ts": "1.0", "user": _OWNER, "channel": "D", "text": "hi"}]
        )

        assert PendingChatInjection.objects.get().overlay == ""

    def test_drops_the_bots_own_message(self) -> None:
        recorded = _recorder().record_events([{"ts": "1.0", "user": "U_BOT_SELF", "channel": "D1", "text": "mine"}])

        assert recorded == 0
        assert PendingChatInjection.objects.count() == 0

    def test_drops_an_on_behalf_post(self) -> None:
        recorded = _recorder().record_events(
            [{"ts": "1.0", "user": _OWNER, "channel": "D1", "text": "posted for me", "api_app_id": "A123"}]
        )

        assert recorded == 0
        assert PendingChatInjection.objects.count() == 0

    def test_unresolvable_identity_records_nothing(self) -> None:
        recorded = _recorder(identity_resolves=False).record_events(
            [{"ts": "1.0", "user": _OWNER, "channel": "D1", "text": "hi"}]
        )

        assert recorded == 0
        assert PendingChatInjection.objects.count() == 0


class TestThreadTsIsRecorded(TestCase):
    """A reply's thread root is the only identity binding it to its question."""

    def test_thread_reply_records_its_root(self) -> None:
        _recorder().record_events(
            [{"ts": "2.0", "thread_ts": "1.0", "user": _OWNER, "channel": "D", "text": "postgres-1"}]
        )

        assert PendingChatInjection.objects.get().thread_ts == "1.0"

    def test_top_level_message_records_no_thread(self) -> None:
        _recorder().record_events([{"ts": "1.0", "user": _OWNER, "channel": "D", "text": "hi"}])

        assert PendingChatInjection.objects.get().thread_ts == ""

    def test_a_thread_root_is_not_a_reply_to_itself(self) -> None:
        """Slack stamps ``thread_ts == ts`` on a root, which references nothing."""
        _recorder().record_events([{"ts": "1.0", "thread_ts": "1.0", "user": _OWNER, "channel": "D", "text": "hi"}])

        assert PendingChatInjection.objects.get().thread_ts == ""


class TestOnlyTheOwnerIsRecorded(TestCase):
    def test_a_colleagues_message_is_not_recorded(self) -> None:
        recorded = _recorder().record_events([{"ts": "1.0", "user": "U0COLLEAGUE1", "channel": "D1", "text": "run it"}])

        assert (recorded, PendingChatInjection.objects.count()) == (0, 0)

    def test_a_registered_operators_message_is_recorded(self) -> None:
        operator = get_user_model().objects.create_superuser("alice", email="", password="unused")
        OperatorIdentity.objects.create(user=operator, platform="slack", handle="U0DEMOUSER1")

        recorded = _recorder().record_events([{"ts": "1.0", "user": "U0DEMOUSER1", "channel": "D1", "text": "hi"}])

        assert (recorded, PendingChatInjection.objects.get().user_id) == (1, "U0DEMOUSER1")

    def test_a_message_with_no_sender_is_not_recorded(self) -> None:
        recorded = _recorder().record_events([{"ts": "1.0", "channel": "D1", "text": "who sent this"}])

        assert (recorded, PendingChatInjection.objects.count()) == (0, 0)

    def test_another_overlays_owner_is_not_this_overlays_owner(self) -> None:
        recorded = _recorder("demo", owner="U0OWNERDEMO1").record_events(
            [{"ts": "1.0", "user": "U_OTHER", "channel": "D1", "text": "from the other overlay's owner"}]
        )

        assert (recorded, PendingChatInjection.objects.count()) == (0, 0)

    def test_nothing_is_recorded_while_no_owner_is_configured(self) -> None:
        recorded = _recorder(owner="").record_events([{"ts": "1.0", "user": "", "channel": "D1", "text": "hi"}])

        assert (recorded, PendingChatInjection.objects.count()) == (0, 0)
