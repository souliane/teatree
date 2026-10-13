"""Listener-downtime catch-up: a replayed Slack event takes the live path and deduplicates against it.

Real ``SlackBotBackend`` over the fake transport, real recorder, real JSONL queues, the
DB task backend for the answer-cycle wake, and the real reaction consumers. Only the
network and the overlay-to-backend resolution are substituted.
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
from teatree.backends.slack.receiver import DeliveredEvents, default_reactions_queue_path
from teatree.core.models import PendingChatInjection
from teatree.loop.scanners.slack_review_intent import SlackReviewIntentScanner
from teatree.loops import timer_reconciler
from teatree.loops.slack_inbound import catch_up_on_connect, record_and_wake_slack_answer
from teatree.types import RawAPIDict
from tests.integration.slack_bridge_e2e.conftest import FakeSlackTransport, serve_history

pytestmark = pytest.mark.integration

_DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops", "cheap"]}}
_OWNER = "U_HUMAN"
_BOT = "B_BOT"
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
