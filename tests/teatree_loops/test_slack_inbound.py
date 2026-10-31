"""The listener's composition root: record an inbound DM BEFORE waking the answer cycle.

The cycle the wake starts reads ``PendingChatInjection.loop_unreplied()``, so a wake
that lands before the row finds an empty queue and stops without re-arming. The
callback records through the shared recorder first, so the woken cycle has a row. The
hello hook and the inbox job bind the catch-up to the same recorder and wake.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import ANY, patch

import django.test
import pytest
from django.tasks import TaskResultStatus
from django_tasks_db.models import DBTaskResult

from teatree.backends.slack.receiver import DeliveredEvents
from teatree.core.models import PendingChatInjection, ReviewAssignment, Ticket
from teatree.loops import slack_inbound, timer_reconciler
from teatree.loops.slack_catchup_job import CatchUpState, SlackCatchUpJob
from teatree.loops.slack_inbound import catch_up_on_connect, record_slack_dm, reset_dm_recorders, slack_catch_up_job
from teatree.slack_catchup import CatchUpSlice
from teatree.types import RawAPIDict

_DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops", "cheap"]}}

_DM = {"type": "message", "channel_type": "im", "ts": "1.0", "user": "U1", "channel": "D1", "text": "ping"}

_FROM_A_COLLEAGUE = {
    "dm": {**_DM, "user": "U_COLLEAGUE"},
    "thread reply": {**_DM, "channel_type": "channel", "channel": "C1", "thread_ts": "0.5", "user": "U_COLLEAGUE"},
    "app mention": {
        "type": "app_mention",
        "ts": "1.0",
        "channel": "C1",
        "user": "U_COLLEAGUE",
        "text": "review https://github.com/o/r/pull/1",
    },
    "reaction": {"type": "reaction_added", "ts": "1.0", "user": "U_COLLEAGUE", "reaction": "eyes"},
    "event without a sender": {"type": "app_mention", "ts": "1.0", "text": "hi"},
}


@dataclass
class FakeMessaging:
    """A messaging backend that records every outbound call it is asked to make."""

    outbound: list[str] = field(default_factory=list)
    user_id: str = "U1"

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
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
        with patch.object(slack_inbound, "messaging_from_overlay", return_value=self.backend):
            slack_inbound.record_and_wake_slack_answer("demo", dict(_DM))

        row = PendingChatInjection.objects.get()
        assert (row.text, row.overlay) == ("ping", "demo")
        assert self._queued_wakes() == 1

    def test_the_listener_posts_nothing_to_slack(self) -> None:
        # Every outbound reply stays in the answer cycle: the hot path writes a row
        # and nothing else, so a 👀 ack can only come from the cycle's CAS.
        with patch.object(slack_inbound, "messaging_from_overlay", return_value=self.backend):
            slack_inbound.record_and_wake_slack_answer("demo", dict(_DM))

        assert self.backend.outbound == []

    def test_an_owner_mention_wakes_without_recording(self) -> None:
        # A mention has no PendingChatInjection row to write; the wake still fires
        # so the cycle drains whatever the mention scanner queued.
        with patch.object(slack_inbound, "messaging_from_overlay", return_value=self.backend):
            slack_inbound.record_and_wake_slack_answer(
                "demo", {"type": "app_mention", "ts": "1.0", "user": "U1", "text": "hi"}
            )

        assert PendingChatInjection.objects.count() == 0
        assert self._queued_wakes() == 1

    def test_an_event_from_a_non_owner_leaves_no_row_and_wakes_nothing(self) -> None:
        for label, event in _FROM_A_COLLEAGUE.items():
            with self.subTest(label), patch.object(slack_inbound, "messaging_from_overlay", return_value=self.backend):
                slack_inbound.record_and_wake_slack_answer("demo", dict(event))

        assert (
            PendingChatInjection.objects.count(),
            ReviewAssignment.objects.count(),
            Ticket.objects.count(),
            self._queued_wakes(),
        ) == (0, 0, 0, 0)

    def test_a_non_owner_drop_is_logged_without_message_text(self) -> None:
        event = {**_DM, "user": "U_COLLEAGUE", "text": "private message"}

        with (
            patch.object(slack_inbound, "messaging_from_overlay", return_value=self.backend),
            self.assertLogs("teatree.loops.slack_inbound", level="INFO") as logs,
        ):
            slack_inbound.record_and_wake_slack_answer("demo", event)

        assert any("non-owner" in line for line in logs.output)
        assert all("private message" not in line for line in logs.output)

    def test_a_blank_owner_id_drops_every_event_type(self) -> None:
        self.backend.user_id = ""
        for label, event in {**_FROM_A_COLLEAGUE, "owner-shaped dm": dict(_DM)}.items():
            with self.subTest(label), patch.object(slack_inbound, "messaging_from_overlay", return_value=self.backend):
                slack_inbound.record_and_wake_slack_answer("demo", dict(event))

        assert (PendingChatInjection.objects.count(), self._queued_wakes()) == (0, 0)

    def test_an_app_posted_non_dm_event_still_wakes(self) -> None:
        event = {"type": "app_mention", "ts": "1.0", "user": "U1", "text": "hi", "api_app_id": "A_OTHER"}
        with patch.object(slack_inbound, "messaging_from_overlay", return_value=self.backend):
            slack_inbound.record_and_wake_slack_answer("demo", event)

        assert self._queued_wakes() == 1

    def test_a_channel_message_is_not_recorded_as_a_dm(self) -> None:
        with patch.object(slack_inbound, "messaging_from_overlay", return_value=self.backend):
            slack_inbound.record_and_wake_slack_answer("demo", {**_DM, "channel_type": "channel"})

        assert PendingChatInjection.objects.count() == 0

    def test_the_bots_own_dm_is_filtered_out(self) -> None:
        # The hot path shares the sweep's write-side filters, so the bot's own
        # outbound DM never becomes a row the answer cycle would reply to.
        with patch.object(slack_inbound, "messaging_from_overlay", return_value=self.backend):
            slack_inbound.record_and_wake_slack_answer("demo", {**_DM, "user": "U_BOT_SELF"})

        assert PendingChatInjection.objects.count() == 0

    def test_an_unrelated_app_posted_reply_does_not_wake_the_answer_cycle(self) -> None:
        event = {**_DM, "ts": "2.0", "thread_ts": "1.0", "api_app_id": "A_OTHER"}
        with patch.object(slack_inbound, "messaging_from_overlay", return_value=self.backend):
            slack_inbound.record_and_wake_slack_answer("demo", event)

        assert PendingChatInjection.objects.count() == 0
        assert self._queued_wakes() == 0

    def test_an_unresolvable_backend_still_wakes(self) -> None:
        # The sweep is the recovery net: the JSONL write already happened, so the
        # callback degrades to a wake rather than raising into the receiver.
        with patch.object(slack_inbound, "messaging_from_overlay", return_value=None):
            slack_inbound.record_and_wake_slack_answer("demo", dict(_DM))

        assert PendingChatInjection.objects.count() == 0
        assert self._queued_wakes() == 1

    def test_an_unresolvable_backend_is_re_probed_on_the_next_event(self) -> None:
        # Memoising a None would wedge the hot path for the process's lifetime.
        with patch.object(slack_inbound, "messaging_from_overlay", return_value=None):
            slack_inbound.record_and_wake_slack_answer("demo", dict(_DM))
        with patch.object(slack_inbound, "messaging_from_overlay", return_value=self.backend):
            slack_inbound.record_and_wake_slack_answer("demo", dict(_DM))

        assert PendingChatInjection.objects.get().text == "ping"

    def test_the_recorder_is_memoised_per_overlay(self) -> None:
        # Rebuilding it per event re-probes the bot's identity on every inbound DM.
        with patch.object(slack_inbound, "messaging_from_overlay", return_value=self.backend) as resolve:
            slack_inbound.record_and_wake_slack_answer("demo", dict(_DM))
            slack_inbound.record_and_wake_slack_answer("demo", {**_DM, "ts": "2.0"})

        assert resolve.call_count == 1
        assert PendingChatInjection.objects.count() == 2

    def test_the_roster_reset_empties_the_memo_so_the_next_resolve_rebuilds(self) -> None:
        # The memo outlives the backend cache it derives from, so conftest resets it
        # around every test; an entry kept here answers the next test's overlay with
        # this test's backend.
        with patch.object(slack_inbound, "messaging_from_overlay", return_value=self.backend):
            slack_inbound.record_and_wake_slack_answer("demo", dict(_DM))
        assert "demo" in slack_inbound._dm_recorders

        reset_dm_recorders()

        assert slack_inbound._dm_recorders == {}
        replacement = FakeMessaging()
        with patch.object(slack_inbound, "messaging_from_overlay", return_value=replacement) as resolve:
            slack_inbound.record_and_wake_slack_answer("demo", {**_DM, "ts": "2.0"})
        assert resolve.call_count == 1


class TestRecordSlackDm(django.test.TestCase):
    def test_reports_true_only_for_a_new_row(self) -> None:
        with patch.object(slack_inbound, "messaging_from_overlay", return_value=FakeMessaging()):
            first = record_slack_dm("demo", dict(_DM))
            again = record_slack_dm("demo", dict(_DM))

        assert (first, again) == (True, False)

    def test_a_non_dm_event_is_not_recorded(self) -> None:
        with patch.object(slack_inbound, "messaging_from_overlay", return_value=FakeMessaging()):
            assert record_slack_dm("demo", {"type": "reaction_added", "ts": "1.0"}) is False


class TestCatchUpWiring:
    def test_the_hello_hook_binds_the_recorder_and_the_wake(self) -> None:
        with patch.object(slack_inbound, "run_catch_up") as run:
            catch_up_on_connect("demo", 5.0)

        run.assert_called_once_with(
            "demo", alive_at=5.0, seen=ANY, record_dm=record_slack_dm, wake=timer_reconciler.wake_slack_answer.enqueue
        )

    def test_the_inbox_job_binds_the_recorder_and_the_wake(self) -> None:
        job = slack_catch_up_job("demo")

        assert job == SlackCatchUpJob(
            overlay="demo", record=record_slack_dm, wake=timer_reconciler.wake_slack_answer.enqueue
        )

    def test_hello_preloads_and_persists_only_the_delivery_memo(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        with patch.object(CatchUpState, "path", return_value=path):
            state = CatchUpState(caught_up_at=8.0)
            state.seen.update({"message:D:1.0": 1.0})
            state.save("demo")
            seen = DeliveredEvents()
            with (
                patch.object(slack_inbound, "run_catch_up", return_value=iter([CatchUpSlice(9.0, 0.0, 1)])) as run,
                patch.object(CatchUpState, "save", side_effect=AssertionError("hello must not advance the cursor")),
            ):
                catch_up_on_connect("demo", 5.0, seen)

            assert run.call_args.kwargs["alive_at"] == pytest.approx(8.0)
            assert run.call_args.kwargs["seen"] is seen
            assert seen.as_mapping() == {"message:D:1.0": 1.0}
            assert CatchUpState.load_memo("demo") == {"message:D:1.0": 1.0}
            stamp = f"{time.time():.6f}"
            seen.deliver_once({"type": "message", "channel": "D", "ts": stamp}, lambda: True)
            assert CatchUpState.load_memo("demo") == {f"message:D:{stamp}": float(stamp)}
