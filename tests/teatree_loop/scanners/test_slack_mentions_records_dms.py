"""A DM drained from the Socket Mode queue is recorded even when its live record failed."""

import json
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase

from teatree.backends.slack import receiver
from teatree.core.models import PendingChatInjection
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.scanners.slack_mentions import SlackMentionsScanner
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
    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return f"https://slack.example.com/{channel}/{ts}"

    def auth_test(self) -> RawAPIDict:
        return {"ok": True, "user_id": "U_BOT", "bot_id": "B_BOT"}


class TestDrainedDmsAreRecorded(TestCase):
    def setUp(self) -> None:
        self.queue = Path(self.enterContext(TemporaryDirectory())) / "slack-events.jsonl"
        self.enterContext(patch.object(receiver, "default_queue_path", return_value=self.queue))

    def _enqueue(self, *events: RawAPIDict) -> None:
        with self.queue.open("a", encoding="utf-8") as queue:
            queue.writelines(json.dumps({"overlay": "demo", "event": event}) + "\n" for event in events)

    @staticmethod
    def _scan() -> None:
        SlackMentionsScanner(backend=FakeMessaging(), overlay="demo").scan()

    def test_a_dm_whose_live_record_failed_is_recorded_by_the_next_drain(self) -> None:
        self._enqueue(_HUMAN_DM)
        assert PendingChatInjection.objects.count() == 0

        self._scan()

        row = PendingChatInjection.objects.get()
        assert (row.slack_ts, row.text, row.overlay) == (_HUMAN_DM_TS, "ping", "demo")

        self._enqueue(_HUMAN_DM)
        self._scan()

        assert PendingChatInjection.objects.count() == 1

    def test_the_bots_own_drained_dm_is_not_recorded(self) -> None:
        self._enqueue(_BOT_DM, _HUMAN_DM)

        self._scan()

        assert list(PendingChatInjection.objects.values_list("slack_ts", flat=True)) == [_HUMAN_DM_TS]

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
        scanner.scan()

        assert list(PendingChatInjection.objects.values_list("slack_ts", flat=True)) == ["100.4"]
        assert [record["event"]["ts"] for record in receiver.drain_event_queue()] == ["100.3"]

        backend.read_error = False
        scanner.scan()

        assert set(PendingChatInjection.objects.values_list("slack_ts", flat=True)) == {"100.3", "100.4"}
