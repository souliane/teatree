"""Listener-downtime catch-up: a replayed Slack event takes the live path and deduplicates against it.

Real ``SlackBotBackend`` over the fake transport, real recorder, real JSONL queues, the
DB task backend for the answer-cycle wake, and the real reaction consumers. Only the
network and the overlay-to-backend resolution are substituted. Live and replayed DMs share
one sender policy: only the configured owner's DM becomes owner work.
"""

import json
import logging
import time
from typing import cast

import django.test
import pytest
from django.tasks import TaskResultStatus
from django_tasks_db.models import DBTaskResult

from teatree.backends.slack.bot import SlackBotBackend
from teatree.backends.slack.receiver import (
    DeliveredEvents,
    DeliveryContext,
    QueuePaths,
    default_queue_path,
    default_reactions_queue_path,
    deliver_event,
)
from teatree.core.models import DmContext, PendingChatInjection, Task
from teatree.loop.dispatch import dispatch
from teatree.loop.inbound_reading import InboundIntent, InboundReading, ReadingSource
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.slack_mentions import SlackMentionsScanner
from teatree.loop.scanners.slack_review_intent import SlackReviewIntentScanner
from teatree.loop.slack_answer.cycle import SlackAnswerReport, run_slack_answer_cycle
from teatree.loops import timer_reconciler
from teatree.loops.slack_inbound import catch_up_on_connect, record_and_wake_slack_answer
from teatree.types import RawAPIDict
from tests.integration.slack_bridge_e2e.conftest import FakeSlackTransport, serve_history

pytestmark = pytest.mark.integration

_DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops", "cheap"]}}
_OWNER = "U_HUMAN"
_COLLEAGUE = "U_COLLEAGUE"
_BOT = "B_BOT"
_PR_REQUEST = "please review https://github.com/acme/app/pull/7 right now"
_DM = "D-USER"
_DAY = 86400.0


def _ago(seconds: float) -> str:
    return f"{time.time() - seconds:.6f}"


def _live(ts: str, text: str) -> RawAPIDict:
    return {"type": "message", "channel_type": "im", "channel": _DM, "user": _OWNER, "text": text, "ts": ts}


def _row_shape(row: PendingChatInjection) -> tuple[str, str, str, str, str, str]:
    return row.overlay, row.channel, row.slack_ts, row.text, row.user_id, row.thread_ts


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class CatchUpCase(django.test.TestCase):
    transport: FakeSlackTransport
    backend: SlackBotBackend

    @pytest.fixture(autouse=True)
    def _slack(self, transport: FakeSlackTransport, overlay_backend: SlackBotBackend) -> None:
        self.transport = transport
        self.backend = overlay_backend

    @staticmethod
    def _wakes() -> int:
        return DBTaskResult.objects.filter(
            task_path=timer_reconciler.wake_slack_answer.module_path, status=TaskResultStatus.READY
        ).count()

    @staticmethod
    def _queued_reactions() -> list[RawAPIDict]:
        path = default_reactions_queue_path()
        if not path.exists():
            return []
        return [json.loads(line)["event"] for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestAMissedDmTakesTheLivePath(CatchUpCase):
    def test_a_missed_dm_is_recorded_and_wakes_once(self) -> None:
        ts = _ago(30)
        serve_history(self.transport, [{"ts": ts, "user": _OWNER, "text": "ship it"}])

        catch_up_on_connect("demo", time.time() - 120)

        replayed = _row_shape(PendingChatInjection.objects.get())
        assert self._wakes() == 1
        PendingChatInjection.objects.all().delete()
        record_and_wake_slack_answer("demo", _live(ts, "ship it"))
        assert replayed == _row_shape(PendingChatInjection.objects.get())

    def test_catching_up_twice_or_beside_a_live_copy_leaves_one_row(self) -> None:
        ts = f"{int(time.time()) - 30}.0001"
        serve_history(self.transport, [{"ts": ts, "user": _OWNER, "text": "ship it"}])
        record_and_wake_slack_answer("demo", _live(ts, "ship it"))

        catch_up_on_connect("demo", time.time() - 120)
        catch_up_on_connect("demo", time.time() - 120)

        assert list(PendingChatInjection.objects.values_list("slack_ts", flat=True)) == [ts]

    def test_three_missed_dms_enqueue_one_wake(self) -> None:
        serve_history(
            self.transport,
            [{"ts": _ago(seconds), "user": _OWNER, "text": f"dm {seconds}"} for seconds in (40, 30, 20)],
        )

        catch_up_on_connect("demo", time.time() - 120)

        assert PendingChatInjection.objects.count() == 3
        assert self._wakes() == 1

    def test_a_pass_that_records_nothing_new_does_not_wake(self) -> None:
        ts = _ago(30)
        serve_history(self.transport, [{"ts": ts, "user": _OWNER, "text": "ship it"}])
        PendingChatInjection.record(channel=_DM, slack_ts=ts, text="ship it", overlay="demo")

        catch_up_on_connect("demo", time.time() - 120)

        assert self._wakes() == 0

    def test_the_bots_own_dm_is_not_recorded(self) -> None:
        serve_history(self.transport, [{"ts": _ago(30), "user": _BOT, "text": "a status DM"}])

        catch_up_on_connect("demo", time.time() - 120)

        assert PendingChatInjection.objects.count() == 0
        assert self._wakes() == 0


class TestTheWindowStartsAtTheLastAliveInstant(CatchUpCase):
    def test_a_missed_dm_before_a_newer_live_row_is_recovered(self) -> None:
        missed, live = _ago(200), _ago(100)
        serve_history(
            self.transport,
            [{"ts": missed, "user": _OWNER, "text": "missed"}, {"ts": live, "user": _OWNER, "text": "live"}],
        )
        record_and_wake_slack_answer("demo", _live(live, "live"))

        catch_up_on_connect("demo", time.time() - 300)

        assert PendingChatInjection.objects.get(slack_ts=missed).text == "missed"

    def test_a_reply_under_a_root_older_than_the_window_is_recovered(self) -> None:
        root, reply = _ago(3 * 3600), _ago(100)
        serve_history(
            self.transport,
            [{"ts": root, "user": _OWNER, "text": "an old question"}],
            replies={root: [{"ts": reply, "user": _OWNER, "text": "the follow-up"}]},
        )

        catch_up_on_connect("demo", time.time() - 300)

        row = PendingChatInjection.objects.get()
        assert (row.slack_ts, row.thread_ts, row.text) == (reply, root, "the follow-up")

    def test_alive_at_in_the_future_is_clamped_to_now(self) -> None:
        ts = _ago(30)
        serve_history(self.transport, [{"ts": ts, "user": _OWNER, "text": "ship it"}])

        catch_up_on_connect("demo", time.time() + 3600)

        assert PendingChatInjection.objects.get().slack_ts == ts

    def test_alive_at_older_than_the_cap_is_clamped_and_the_lost_span_is_logged(self) -> None:
        kept, lost = _ago(6 * _DAY), _ago(7 * _DAY + 7200)
        serve_history(
            self.transport,
            [{"ts": lost, "user": _OWNER, "text": "lost"}, {"ts": kept, "user": _OWNER, "text": "kept"}],
        )

        with self.assertLogs("teatree.slack_catchup", level=logging.WARNING) as logs:
            catch_up_on_connect("demo", time.time() - 10 * _DAY)

        assert list(PendingChatInjection.objects.values_list("text", flat=True)) == ["kept"]
        assert any("cannot recover" in line for line in logs.output)


class TestOwnerReactionsAreReplayed(CatchUpCase):
    def _bot_dm_with_owner_reaction(self) -> str:
        ts = _ago(3 * 3600)
        serve_history(
            self.transport,
            [
                {
                    "ts": ts,
                    "user": _BOT,
                    "text": "PR ready",
                    "reactions": [{"name": "+1", "users": [_OWNER], "count": 1}],
                }
            ],
        )
        return ts

    def test_an_owner_reaction_added_during_downtime_is_acked_once(self) -> None:
        ts = self._bot_dm_with_owner_reaction()

        catch_up_on_connect("demo", time.time() - 300)
        SlackReviewIntentScanner(backend=self.backend, overlay="demo").scan()
        catch_up_on_connect("demo", time.time() - 300)
        SlackReviewIntentScanner(backend=self.backend, overlay="demo").scan()

        acks = [c.payload for c in self.transport.calls_to("reactions.add")]
        assert [(p["channel"], p["timestamp"], p["name"]) for p in acks] == [(_DM, ts, "eyes")]

    def test_a_replayed_reaction_is_delivered_once_per_process(self) -> None:
        ts = self._bot_dm_with_owner_reaction()
        seen = DeliveredEvents()

        catch_up_on_connect("demo", time.time() - 300, seen)
        catch_up_on_connect("demo", time.time() - 300, seen)

        assert [(e["user"], e["reaction"], cast("RawAPIDict", e["item"])["ts"]) for e in self._queued_reactions()] == [
            (_OWNER, "+1", ts)
        ]

    def test_a_colleague_reaction_is_not_replayed(self) -> None:
        serve_history(
            self.transport,
            [{"ts": _ago(600), "user": _BOT, "text": "hi", "reactions": [{"name": "+1", "users": ["U_OTHER"]}]}],
        )

        catch_up_on_connect("demo", time.time() - 300)

        assert self._queued_reactions() == []


def _asks_for_work(_text: str) -> InboundReading:
    return InboundReading(
        intent=InboundIntent.INSTRUCTION,
        answerable=False,
        work_summary="act on the request",
        source=ReadingSource.MODEL,
        rationale="test fixture",
    )


class TestOnlyTheOwnersDmBecomesOwnerWork(CatchUpCase):
    def _over_the_socket(self, user: str, ts: str, *, text: str = _PR_REQUEST) -> list[ScanSignal]:
        context = DeliveryContext(
            queues=QueuePaths(events=default_queue_path(), reactions=default_reactions_queue_path()),
            identity=None,
            on_event=record_and_wake_slack_answer,
            seen=DeliveredEvents(),
        )
        assert deliver_event("demo", {**_live(ts, text), "user": user}, context=context)
        return SlackMentionsScanner(backend=self.backend, overlay="demo").scan()

    def _answer_cycle(self) -> SlackAnswerReport:
        return run_slack_answer_cycle(messaging_resolver=lambda _overlay: self.backend, reader=_asks_for_work)

    def test_a_colleagues_dm_over_the_socket_never_becomes_owner_work(self) -> None:
        signals = self._over_the_socket(_COLLEAGUE, _ago(30))

        actions = dispatch(signals)
        report = self._answer_cycle()

        assert (signals, actions, report.processed) == ([], [], 0)
        assert (PendingChatInjection.objects.count(), self._wakes(), Task.objects.count()) == (0, 0, 0)
        assert not [c for c in self.transport.calls if c.method in {"reactions.add", "chat.postMessage"}]

    def test_a_long_colleague_dm_creates_no_owner_work(self) -> None:
        signals = self._over_the_socket(_COLLEAGUE, _ago(30), text="do this " * 2000)

        assert signals == []
        assert self._answer_cycle().processed == 0
        assert (PendingChatInjection.objects.count(), self._wakes(), Task.objects.count()) == (0, 0, 0)

    def test_colleague_dm_with_an_owner_timestamp_cannot_replace_the_owner_row(self) -> None:
        ts = _ago(30)
        self._over_the_socket(_OWNER, ts)

        signals = self._over_the_socket(_COLLEAGUE, ts, text="replace the owner request")

        assert signals == []
        assert list(PendingChatInjection.objects.values_list("user_id", "text")) == [(_OWNER, _PR_REQUEST)]
        assert self._wakes() == 1

    def test_the_owners_dm_over_the_same_path_is_one_row_one_wake_one_task(self) -> None:
        signals = self._over_the_socket(_OWNER, _ago(30))

        report = self._answer_cycle()

        assert [signal.kind for signal in signals] == ["slack.dm"]
        assert (PendingChatInjection.objects.count(), self._wakes()) == (1, 1)
        assert (report.dispatched, Task.objects.filter(status=Task.Status.PENDING).count()) == (1, 1)

    def test_legacy_foreign_rows_stay_inert_while_a_later_owner_row_is_answered(self) -> None:
        for number in range(12):
            PendingChatInjection.record(
                channel=_DM,
                slack_ts=f"{number + 1}.0",
                text="legacy request",
                overlay="demo",
                context=DmContext(user_id=_COLLEAGUE),
            )
        PendingChatInjection.record(
            channel=_DM, slack_ts=_ago(30), text="owner request", overlay="demo", context=DmContext(user_id=_OWNER)
        )

        report = self._answer_cycle()

        assert report.dispatched == 1
        assert PendingChatInjection.objects.filter(user_id=_COLLEAGUE, loop_replied_at__isnull=False).count() == 0
        assert {c.payload["timestamp"] for c in self.transport.calls_to("reactions.add")}.isdisjoint(
            {f"{number + 1}.0" for number in range(12)}
        )


class TestCatchUpAppliesTheSameSenderDecision(CatchUpCase):
    def test_only_the_owners_message_in_the_history_is_recorded(self) -> None:
        owner_ts = _ago(40)
        serve_history(
            self.transport,
            [
                {"ts": owner_ts, "user": _OWNER, "text": "ship it"},
                {"ts": _ago(30), "user": _COLLEAGUE, "text": "run my job"},
                {"ts": _ago(20), "text": "a message with no sender"},
            ],
        )

        catch_up_on_connect("demo", time.time() - 120)

        assert list(PendingChatInjection.objects.values_list("slack_ts", flat=True)) == [owner_ts]
        assert self._wakes() == 1
