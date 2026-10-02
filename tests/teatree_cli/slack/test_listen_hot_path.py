"""The listener records an inbound DM BEFORE it wakes the answer cycle.

The wake was already wired, but the cycle it wakes reads
``PendingChatInjection.loop_unreplied()``, whose only writer was the ~60s inbox
sweep. So the wake fired ~1s after arrival, found no row, and stopped without
re-arming — the answer then waited out the sweep plus the 5m fallback. The
callback now records the row through the sweep's own ``record_events`` seam
first, so the woken cycle has something to read.
"""

from dataclasses import dataclass, field
from unittest.mock import patch

import django.test
from django.tasks import TaskResultStatus
from django_tasks_db.models import DBTaskResult

from teatree.cli.slack import listen
from teatree.cli.slack.listen import reset_dm_recorders
from teatree.core.models import PendingChatInjection
from teatree.loops import timer_reconciler
from teatree.types import RawAPIDict

_DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops", "cheap"]}}

_DM = {"type": "message", "channel_type": "im", "ts": "1.0", "user": "U1", "channel": "D1", "text": "ping"}


@dataclass
class FakeMessaging:
    """A messaging backend that records every outbound call it is asked to make."""

    outbound: list[str] = field(default_factory=list)

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        self.outbound.append("fetch_dms")
        return []

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        self.outbound.append("post_message")
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        _ = (channel, ts, text)
        self.outbound.append("post_reply")
        return {}

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        return "D1"

    def get_permalink(self, *, channel: str, ts: str) -> str:
        _ = (channel, ts)
        return ""

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        _ = (channel, ts, emoji)
        self.outbound.append("react")
        return {}

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""

    def auth_test(self) -> RawAPIDict:
        return {"ok": True, "user_id": "U_BOT_SELF", "bot_id": "B_BOT_SELF"}


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestRecordThenWake(django.test.TestCase):
    def setUp(self) -> None:
        self.backend = FakeMessaging()

    @staticmethod
    def _queued_wakes() -> int:
        return DBTaskResult.objects.filter(
            task_path=timer_reconciler.wake_slack_answer.module_path, status=TaskResultStatus.READY
        ).count()

    def test_a_dm_is_recorded_before_the_wake_fires(self) -> None:
        with patch.object(listen, "messaging_from_overlay", return_value=self.backend):
            listen._record_and_wake_slack_answer("demo", dict(_DM))

        row = PendingChatInjection.objects.get()
        assert (row.text, row.overlay) == ("ping", "demo")
        assert self._queued_wakes() == 1

    def test_the_listener_posts_nothing_to_slack(self) -> None:
        # Every outbound reply stays in the answer cycle: the hot path writes a row
        # and nothing else, so a 👀 ack can only come from the cycle's CAS.
        with patch.object(listen, "messaging_from_overlay", return_value=self.backend):
            listen._record_and_wake_slack_answer("demo", dict(_DM))

        assert self.backend.outbound == []

    def test_a_non_dm_event_wakes_without_recording(self) -> None:
        # A mention has no PendingChatInjection row to write; the wake still fires
        # so the cycle drains whatever the mention scanner queued.
        with patch.object(listen, "messaging_from_overlay", return_value=self.backend):
            listen._record_and_wake_slack_answer("demo", {"type": "app_mention", "ts": "1.0", "text": "hi"})

        assert PendingChatInjection.objects.count() == 0
        assert self._queued_wakes() == 1

    def test_a_channel_message_is_not_recorded_as_a_dm(self) -> None:
        with patch.object(listen, "messaging_from_overlay", return_value=self.backend):
            listen._record_and_wake_slack_answer("demo", {**_DM, "channel_type": "channel"})

        assert PendingChatInjection.objects.count() == 0

    def test_the_bots_own_dm_is_filtered_out(self) -> None:
        # The hot path shares the sweep's write-side filters, so the bot's own
        # outbound DM never becomes a row the answer cycle would reply to.
        with patch.object(listen, "messaging_from_overlay", return_value=self.backend):
            listen._record_and_wake_slack_answer("demo", {**_DM, "user": "U_BOT_SELF"})

        assert PendingChatInjection.objects.count() == 0

    def test_an_unresolvable_backend_still_wakes(self) -> None:
        # The sweep is the recovery net: the JSONL write already happened, so the
        # callback degrades to a wake rather than raising into the receiver.
        with patch.object(listen, "messaging_from_overlay", return_value=None):
            listen._record_and_wake_slack_answer("demo", dict(_DM))

        assert PendingChatInjection.objects.count() == 0
        assert self._queued_wakes() == 1

    def test_an_unresolvable_backend_is_re_probed_on_the_next_event(self) -> None:
        # Memoising a None would wedge the hot path for the process's lifetime.
        with patch.object(listen, "messaging_from_overlay", return_value=None):
            listen._record_and_wake_slack_answer("demo", dict(_DM))
        with patch.object(listen, "messaging_from_overlay", return_value=self.backend):
            listen._record_and_wake_slack_answer("demo", dict(_DM))

        assert PendingChatInjection.objects.get().text == "ping"

    def test_the_recorder_is_memoised_per_overlay(self) -> None:
        # Rebuilding it per event re-probes the bot's identity on every inbound DM.
        with patch.object(listen, "messaging_from_overlay", return_value=self.backend) as resolve:
            listen._record_and_wake_slack_answer("demo", dict(_DM))
            listen._record_and_wake_slack_answer("demo", {**_DM, "ts": "2.0"})

        assert resolve.call_count == 1
        assert PendingChatInjection.objects.count() == 2

    def test_the_roster_reset_empties_the_memo_so_the_next_resolve_rebuilds(self) -> None:
        # The memo outlives the backend cache it derives from, so conftest resets it
        # around every test; an entry kept here answers the next test's overlay with
        # this test's backend.
        with patch.object(listen, "messaging_from_overlay", return_value=self.backend):
            listen._record_and_wake_slack_answer("demo", dict(_DM))
        assert "demo" in listen._dm_recorders

        reset_dm_recorders()

        assert listen._dm_recorders == {}
        replacement = FakeMessaging()
        with patch.object(listen, "messaging_from_overlay", return_value=replacement) as resolve:
            listen._record_and_wake_slack_answer("demo", {**_DM, "ts": "2.0"})
        assert resolve.call_count == 1
