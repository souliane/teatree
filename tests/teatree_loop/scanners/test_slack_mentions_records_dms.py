"""A DM drained from the Socket Mode queue is recorded even when its live record failed."""

import json
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase

from teatree.backends.slack import receiver
from teatree.core.models import PendingChatInjection
from teatree.loop.scanners.slack_mentions import SlackMentionsScanner
from teatree.types import RawAPIDict

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
