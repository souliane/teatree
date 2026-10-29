"""A DM drained from the Socket Mode queue is recorded even when its live record failed."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase

from teatree.backends.slack import receiver
from teatree.core.models import PendingChatInjection, ReviewAssignment
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.scanners.slack_dm_recorder import SlackDmRecorder
from teatree.loop.scanners.slack_mentions import SlackMentionsScanner
from teatree.loops import slack_inbound
from teatree.types import RawAPIDict
from tests._owner_channel import OWNER_CARD, OWNER_SLACK_ID

_HUMAN_DM_TS = "1700000000.000100"
_HUMAN_DM: RawAPIDict = {
    "type": "message",
    "channel_type": "im",
    "ts": _HUMAN_DM_TS,
    "user": "U_HUMAN",
    "channel": "D1",
    "text": "ping",
}
_BOT_DM: RawAPIDict = {
    "type": "message",
    "channel_type": "im",
    "ts": "1700000000.000200",
    "user": "U_BOT",
    "channel": "D1",
    "text": "bot says hi",
}


@dataclass
class FakeMessaging:
    user_id: str = "U_HUMAN"

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return f"https://slack.example.com/{channel}/{ts}"

    def auth_test(self) -> RawAPIDict:
        return {"ok": True, "user_id": "U_BOT", "bot_id": "B_BOT"}


_COLLEAGUE_DM: RawAPIDict = {**_HUMAN_DM, "ts": "1700000000.000300", "user": "U_COLLEAGUE", "text": "run my job"}


class TestDrainedDmsAreRecorded(TestCase):
    def setUp(self) -> None:
        self.queue = Path(self.enterContext(TemporaryDirectory())) / "slack-events.jsonl"
        self.enterContext(patch.object(receiver, "default_queue_path", return_value=self.queue))

    def _enqueue(self, *events: RawAPIDict) -> None:
        with self.queue.open("a", encoding="utf-8") as queue:
            queue.writelines(json.dumps({"overlay": "demo", "event": event}) + "\n" for event in events)

    @staticmethod
    def _scan(backend: FakeMessaging | None = None) -> None:
        SlackMentionsScanner(backend=backend or FakeMessaging(), overlay="demo").scan()

    def test_a_dm_whose_live_record_failed_is_recorded_by_the_next_drain(self) -> None:
        self._enqueue(_HUMAN_DM)
        assert PendingChatInjection.objects.count() == 0

        self._scan()

        row = PendingChatInjection.objects.get()
        assert (row.slack_ts, row.text, row.overlay) == (_HUMAN_DM_TS, "ping", "demo")

        self._enqueue(_HUMAN_DM)
        self._scan()

        assert PendingChatInjection.objects.count() == 1

    def test_live_then_drain_logs_one_ignored_app_reply(self) -> None:
        event = {
            **_HUMAN_DM,
            "ts": "101.2",
            "thread_ts": "101.1",
            "user": OWNER_SLACK_ID,
            "api_app_id": "A_OWNER_SCRIPT",
        }
        self._enqueue(event)
        with (
            patch.object(slack_inbound, "messaging_from_overlay", return_value=FakeMessaging(user_id=OWNER_SLACK_ID)),
            patch.object(slack_inbound, "wake_slack_answer"),
            self.assertLogs("teatree.loop.scanners.slack_dm_recorder", level="INFO") as logs,
        ):
            slack_inbound.record_and_wake_slack_answer("demo", event)
            self._scan(FakeMessaging(user_id=OWNER_SLACK_ID))

        assert PendingChatInjection.objects.count() == 0
        assert sum("101.2" in line and "posted through an app token" in line for line in logs.output) == 1

    def test_live_then_drain_reads_a_pending_card_thread_once(self) -> None:
        card = DeferredQuestion.record("Can I ship?", card=OWNER_CARD, slack_channel="D1", slack_ts="102.2")
        event = {
            **_HUMAN_DM,
            "ts": "102.3",
            "thread_ts": "102.1",
            "user": OWNER_SLACK_ID,
            "api_app_id": "A_OWNER_SCRIPT",
        }

        @dataclass
        class ReadingBackend(FakeMessaging):
            user_id: str = OWNER_SLACK_ID
            reads: list[str] = field(default_factory=list)

            def fetch_thread_replies(self, *, channel: str, thread_ts: str) -> list[RawAPIDict]:
                self.reads.append(thread_ts)
                return [{"ts": thread_ts}, {"ts": card.slack_ts}]

        backend = ReadingBackend()
        self._enqueue(event)
        with (
            patch.object(slack_inbound, "messaging_from_overlay", return_value=backend),
            patch.object(slack_inbound, "wake_slack_answer"),
        ):
            slack_inbound.record_and_wake_slack_answer("demo", event)
            SlackMentionsScanner(backend=backend, overlay="demo").scan()

        assert PendingChatInjection.objects.filter(slack_ts="102.3").count() == 1
        assert backend.reads == ["102.1"]

    def test_the_bots_own_drained_dm_is_not_recorded(self) -> None:
        self._enqueue(_BOT_DM, _HUMAN_DM)

        self._scan()

        assert list(PendingChatInjection.objects.values_list("slack_ts", flat=True)) == [_HUMAN_DM_TS]

    def test_a_colleagues_drained_dm_is_neither_recorded_nor_signalled(self) -> None:
        self._enqueue(_COLLEAGUE_DM, _HUMAN_DM)

        signals = SlackMentionsScanner(backend=FakeMessaging(), overlay="demo").scan()

        assert [signal.payload["ts"] for signal in signals] == [_HUMAN_DM_TS]
        assert list(PendingChatInjection.objects.values_list("slack_ts", flat=True)) == [_HUMAN_DM_TS]
        assert not self.queue.exists()

    def test_a_failed_card_thread_read_is_retained_while_another_dm_records(self) -> None:
        card = DeferredQuestion.record("Can I ship?", card=OWNER_CARD, slack_channel="D1", slack_ts="100.2")
        failed = {
            **_HUMAN_DM,
            "ts": "100.3",
            "thread_ts": "100.1",
            "user": OWNER_SLACK_ID,
            "api_app_id": "A_OWNER_SCRIPT",
        }
        direct = {**_HUMAN_DM, "ts": "100.4", "user": OWNER_SLACK_ID}
        self._enqueue(failed, direct)

        @dataclass
        class ReadingBackend(FakeMessaging):
            user_id: str = OWNER_SLACK_ID
            read_error: bool = True

            def fetch_thread_replies(self, *, channel: str, thread_ts: str) -> list[RawAPIDict]:
                if self.read_error:
                    message = "Slack read refused"
                    raise RuntimeError(message)
                return [{"ts": thread_ts}, {"ts": card.slack_ts}]

        backend = ReadingBackend()
        scanner = SlackMentionsScanner(backend=backend, overlay="demo")
        first = scanner.scan()

        assert list(PendingChatInjection.objects.values_list("slack_ts", flat=True)) == ["100.4"]
        assert [signal.payload["ts"] for signal in first if signal.kind == "slack.dm"] == ["100.4"]
        assert [record["event"]["ts"] for record in receiver.drain_event_queue()] == ["100.3"]

        backend.read_error = False
        retried = scanner.scan()

        assert set(PendingChatInjection.objects.values_list("slack_ts", flat=True)) == {"100.3", "100.4"}
        assert [signal.payload["ts"] for signal in retried if signal.kind == "slack.dm"] == ["100.3"]

    def test_a_batch_whose_recording_raised_stays_queued_and_emits_no_dm_signal(self) -> None:
        self._enqueue(_HUMAN_DM)
        scanner = SlackMentionsScanner(backend=FakeMessaging(), overlay="demo")

        with patch.object(SlackDmRecorder, "record_events", side_effect=RuntimeError("database unavailable")):
            signals = scanner.scan()

        assert [signal for signal in signals if signal.kind == "slack.dm"] == []
        assert [record["event"]["ts"] for record in receiver.drain_event_queue()] == [_HUMAN_DM_TS]


_PR_URL = "https://github.com/souliane/teatree/pull/1"


@dataclass
class MentionBacklogMessaging(FakeMessaging):
    mentions: list[RawAPIDict] = field(default_factory=list)

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return list(self.mentions)


def _mention(user: str) -> RawAPIDict:
    return {
        "type": "app_mention",
        "ts": "1700000000.000400",
        "channel": "C1",
        "user": user,
        "text": f"please review {_PR_URL}",
    }


class TestMentionsAreOwnerOnly(TestCase):
    def setUp(self) -> None:
        self.queue = Path(self.enterContext(TemporaryDirectory())) / "slack-events.jsonl"
        self.enterContext(patch.object(receiver, "default_queue_path", return_value=self.queue))

    def _enqueue(self, event: RawAPIDict) -> None:
        self.queue.write_text(json.dumps({"overlay": "demo", "event": event}) + "\n", encoding="utf-8")

    @staticmethod
    def _scan(backend: FakeMessaging) -> list[str]:
        return [signal.kind for signal in SlackMentionsScanner(backend=backend, overlay="demo").scan()]

    def test_a_queued_owner_mention_is_surfaced_and_recorded_as_a_review_intent(self) -> None:
        self._enqueue(_mention("U_HUMAN"))

        kinds = self._scan(FakeMessaging())

        assert kinds == ["slack.mention"]
        assert ReviewAssignment.objects.get().mr_url == _PR_URL

    def test_a_queued_mention_from_a_colleague_is_dropped_with_no_signal_and_no_row(self) -> None:
        self._enqueue(_mention("U_COLLEAGUE"))

        kinds = self._scan(FakeMessaging())

        assert (kinds, ReviewAssignment.objects.count(), self.queue.exists()) == ([], 0, False)

    def test_a_backlog_mention_from_a_colleague_is_dropped_with_no_signal_and_no_row(self) -> None:
        kinds = self._scan(MentionBacklogMessaging(mentions=[_mention("U_COLLEAGUE"), _mention("U_HUMAN")]))

        assert (kinds, ReviewAssignment.objects.count()) == (["slack.mention"], 1)

    def test_a_blank_owner_id_drops_every_mention(self) -> None:
        self._enqueue(_mention("U_HUMAN"))

        kinds = self._scan(MentionBacklogMessaging(user_id="", mentions=[_mention("U_HUMAN")]))

        assert (kinds, ReviewAssignment.objects.count()) == ([], 0)
