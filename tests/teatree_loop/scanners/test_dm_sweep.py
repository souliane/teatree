"""The tick-level owner-DM sweep — watermark, live thread read, and silence (#3658)."""

from dataclasses import dataclass, field
from datetime import timedelta
from unittest import mock

from django.db import OperationalError
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import DeferredQuestion, Loop
from teatree.core.owner_threads import OwnerThread
from teatree.loop.scanners.dm_sweep import DmSweepScanner, SlackThreadReply, _is_owner_reply
from teatree.types import RawAPIDict

CHANNEL = "D0OWNER"
TS = "1779990001.000001"


@dataclass
class _FakeMessaging:
    replies: list[RawAPIDict] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    error: Exception | None = None

    def fetch_thread_replies(self, *, channel: str, thread_ts: str) -> list[RawAPIDict]:
        self.calls.append((channel, thread_ts))
        if self.error is not None:
            raise self.error
        return list(self.replies)


def _thread(text: str = "Ship it?", *, age: timedelta = timedelta(minutes=5)) -> DeferredQuestion:
    row = DeferredQuestion.record(text, slack_channel=CHANNEL, slack_ts=TS)
    DeferredQuestion.objects.filter(pk=row.pk).update(created_at=timezone.now() - age)
    row.refresh_from_db()
    return row


class TestSilenceWhenNothingToDo(TestCase):
    def test_an_empty_queue_emits_no_signal(self) -> None:
        assert DmSweepScanner().scan() == []

    def test_a_pass_that_resolves_nothing_emits_no_signal(self) -> None:
        _thread()
        assert DmSweepScanner(backend=_FakeMessaging()).scan() == []


class TestOwnerReplyProbe(TestCase):
    def test_an_owner_reply_in_the_thread_resolves_it(self) -> None:
        row = _thread()
        backend = _FakeMessaging(replies=[{"ts": "1779990002.000001", "text": "yes, ship"}])

        signals = DmSweepScanner(backend=backend).scan()

        assert [s.kind for s in signals] == ["dm_sweep.resolved"]
        assert backend.calls == [(CHANNEL, TS)]
        row.refresh_from_db()
        assert row.status == DeferredQuestion.STATUS_DISMISSED

    def test_the_bots_own_post_is_not_an_owner_reply(self) -> None:
        row = _thread()
        backend = _FakeMessaging(replies=[{"ts": "1779990002.000001", "text": "Pending question", "bot_id": "B1"}])

        assert DmSweepScanner(backend=backend).scan() == []
        row.refresh_from_db()
        assert row.status == DeferredQuestion.STATUS_PENDING

    def test_an_unreadable_thread_is_left_open(self) -> None:
        row = _thread()
        backend = _FakeMessaging(error=RuntimeError("slack down"))

        assert DmSweepScanner(backend=backend).scan() == []
        row.refresh_from_db()
        assert row.status == DeferredQuestion.STATUS_PENDING


class TestScanIsResilient(TestCase):
    def test_unmigrated_tables_are_skipped_not_crashed(self) -> None:
        with mock.patch("teatree.core.owner_dm_sweep.run_sweep", side_effect=OperationalError("no such table")):
            assert DmSweepScanner().scan() == []

    def test_an_unexpected_sweep_failure_is_swallowed(self) -> None:
        with mock.patch("teatree.core.owner_dm_sweep.run_sweep", side_effect=RuntimeError("boom")):
            assert DmSweepScanner().scan() == []


class TestOwnerReplyProbeSkipsUnaddressableThreads(TestCase):
    def test_a_thread_with_no_slack_coordinates_is_never_probed(self) -> None:
        backend = _FakeMessaging()
        probe = DmSweepScanner(backend=backend)._owner_replied_probe()
        assert probe is not None

        row = DeferredQuestion.record("Ship it?", slack_channel="", slack_ts="")
        assert probe(OwnerThread(question=row)) is False
        assert backend.calls == []


class TestIsOwnerReply:
    def test_a_non_dict_reply_is_never_an_owner_reply(self) -> None:
        assert _is_owner_reply("not a dict", thread_ts="root") is False

    def test_a_typed_human_reply_in_the_thread_counts(self) -> None:
        reply: SlackThreadReply = {"ts": "1779990002.000001", "text": "yes, ship"}
        assert _is_owner_reply(reply, thread_ts="1779990001.000001") is True

    def test_a_bot_post_and_the_root_message_do_not_count(self) -> None:
        bot: SlackThreadReply = {"ts": "1779990002.000001", "text": "Pending question", "bot_id": "B1"}
        assert _is_owner_reply(bot, thread_ts="1779990001.000001") is False
        root: SlackThreadReply = {"ts": "root", "text": "the question"}
        assert _is_owner_reply(root, thread_ts="root") is False


class TestWatermark(TestCase):
    def test_the_pass_reads_the_loops_own_last_run_ledger(self) -> None:
        # The migration seeds this row, so pin its watermark rather than re-creating it.
        Loop.objects.update_or_create(
            name="dm_sweep",
            defaults={
                "delay_seconds": 3600,
                "script": "src/teatree/loops/dm_sweep/loop.py",
                "last_run_at": timezone.now() - timedelta(minutes=30),
            },
        )
        _thread(age=timedelta(days=5))
        backend = _FakeMessaging(replies=[{"ts": "1779990002.000001", "text": "yes"}])

        # Old thread, opened long before the watermark → out of this pass entirely.
        assert DmSweepScanner(backend=backend).scan() == []
        assert backend.calls == []
