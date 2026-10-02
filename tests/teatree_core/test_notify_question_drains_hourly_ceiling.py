"""The question drains share one hourly ceiling on owner DMs: a resumed backlog is paced, never dropped."""

import contextlib
import datetime as dt
import itertools
import os
import threading
from collections.abc import Callable, Iterator
from unittest.mock import MagicMock, patch

from django.db import connections
from django.db.models import F
from django.test import TestCase
from django.utils import timezone

from teatree.core import notify as notify_module
from teatree.core import notify_question_drains as drains_module
from teatree.core.models import BotPing, DeferredQuestion, LoopLease
from teatree.core.notify_question_drains import drain_deferred_questions, drain_unmirrored_deferred_questions
from teatree.loop.scanners.deferred_question_poster import DeferredQuestionPosterScanner
from teatree.loop.scanners.question_backlog_nag import QuestionBacklogNagScanner
from teatree.settings import SQLITE_WRITE_SERIALIZATION_OPTIONS
from teatree.utils.thread_db import close_thread_db_connections
from tests.db_alias import run_racing_threads
from tests.teatree_core.conftest import SchemaGuardAlias
from tests.teatree_core.test_claim_liveness import _OTHER_NS, _READER_NS, pinned_reader_namespace

_CHANNEL = "D-USER"
_QUESTION_PING_SLOT = "work:owner-question-ping"
_FOREIGN_PID = 4_000_000
_DEAD_PID = 4_100_000


class _NoInProcessGuard:
    """Separate processes share no in-process lock."""

    def acquire(self, *, blocking: bool = True) -> bool:
        return True

    def release(self) -> None:
        return None


@contextlib.contextmanager
def _as_another_live_process() -> Iterator[None]:
    own_pid = os.getpid()
    with (
        patch.object(drains_module, "_QUESTION_PING_THREAD_GUARD", threading.Lock(), create=True),
        patch("os.getpid", return_value=_FOREIGN_PID),
        patch("teatree.utils.singleton.pid_alive", lambda pid: pid in {own_pid, _FOREIGN_PID}),
    ):
        yield


def _backend() -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = _CHANNEL
    stamps = itertools.count(1_800_000_000)
    backend.post_message.side_effect = lambda *_args, **_kwargs: {"ok": True, "ts": f"{next(stamps)}.000000"}
    backend.get_permalink.return_value = "https://acme.slack.com/archives/D-USER/p1800000000000000"
    return backend


def _backlog(question: str, *, slack_ts: str = "", age_days: int = 3) -> DeferredQuestion:
    mirror = {"slack_channel": _CHANNEL, "slack_ts": slack_ts} if slack_ts else {}
    row = DeferredQuestion.record(question, session_id="s", **mirror)
    DeferredQuestion.objects.filter(pk=row.pk).update(created_at=timezone.now() - dt.timedelta(days=age_days))
    row.refresh_from_db()
    return row


def _backend_holding_each_send_open(*, senders: int) -> MagicMock:
    backend = _backend()
    stamps = itertools.count(1_900_000_000)
    in_flight = threading.Barrier(senders, timeout=1)

    def post_message(*_args: object, **_kwargs: object) -> dict[str, object]:
        with contextlib.suppress(threading.BrokenBarrierError):
            in_flight.wait()
        return {"ok": True, "ts": f"{next(stamps)}.000000"}

    backend.post_message.side_effect = post_message
    return backend


def _seed_sent_question_pings(count: int) -> None:
    for index in range(count):
        BotPing.objects.create(
            idempotency_key=f"reask:seeded-{index}:1",
            kind=BotPing.Kind.QUESTION,
            status=BotPing.Status.SENT,
            audience="owner_question",
            text="Still waiting on this one",
        )


def _age_the_ledger(*, minutes: int) -> None:
    BotPing.objects.update(posted_at=F("posted_at") - dt.timedelta(minutes=minutes))


def _sent_pings() -> int:
    return BotPing.objects.filter(status=BotPing.Status.SENT).count()


class TestTheQuestionDrainsShareAnHourlyCeiling(TestCase):
    def setUp(self) -> None:
        self.backend = _backend()
        messaging = patch.object(notify_module, "messaging_from_overlay", return_value=self.backend)
        messaging.start()
        self.addCleanup(messaging.stop)

    def _poster(self) -> None:
        DeferredQuestionPosterScanner(backend=self.backend, user_id="U_ME").scan()

    def _nag(self) -> None:
        QuestionBacklogNagScanner(backend=self.backend, user_id="U_ME").scan()

    def _tick(self) -> None:
        self._poster()
        self._nag()
        _age_the_ledger(minutes=5)

    def test_an_hour_of_ticks_over_a_resumed_41_question_backlog_sends_six_pings(self) -> None:
        for index in range(20):
            _backlog(f"Unposted {index}?")
        for index in range(21):
            _backlog(f"Posted {index}?", slack_ts=f"{100 + index}.0")

        for _ in range(12):
            self._tick()

        assert _sent_pings() == 6

    def test_the_held_questions_are_posted_once_the_hour_rolls_over(self) -> None:
        for index in range(8):
            _backlog(f"Unposted {index}?")

        for _ in range(3):
            self._tick()
        assert DeferredQuestion.unmirrored_pending().count() == 2

        _age_the_ledger(minutes=60)
        self._tick()

        assert DeferredQuestion.unmirrored_pending().count() == 0

    def test_a_new_question_is_posted_ahead_of_re_asks_in_a_nearly_spent_hour(self) -> None:
        for index in range(10):
            _backlog(f"Posted {index}?", slack_ts=f"{100 + index}.0")
        _seed_sent_question_pings(4)
        new_question = _backlog("Which merge target for the hotfix?", age_days=0)

        self._nag()
        self._poster()

        new_question.refresh_from_db()
        assert new_question.slack_ts
        assert _sent_pings() == 6

    def test_a_manual_resurface_in_a_spent_hour_posts_nothing_and_keeps_the_backlog(self) -> None:
        _seed_sent_question_pings(6)
        _backlog("Which DB host?")
        _backlog("Which region?")

        assert drain_deferred_questions(user_id="U_ME", backend=self.backend) == (0, 2)
        self.backend.post_message.assert_not_called()
        assert DeferredQuestion.unmirrored_pending().count() == 2

    def test_never_posted_questions_the_poster_cannot_deliver_do_not_mute_the_nag(self) -> None:
        for index in range(14):
            _backlog(f"Unposted {index}?")
        for index in range(5):
            _backlog(f"Posted {index}?", slack_ts=f"{100 + index}.0")

        self._nag()

        assert _sent_pings() == 3

    def test_a_failed_send_keeps_the_slot_free_and_the_question_is_posted_on_the_next_pass(self) -> None:
        _seed_sent_question_pings(5)
        question = _backlog("Which DB host?")
        self.backend.post_message.side_effect = [
            {"ok": False, "error": "ratelimited"},
            {"ok": True, "ts": "1800000001.000000"},
        ]

        self._poster()
        question.refresh_from_db()
        assert question.slack_ts == ""

        self._poster()
        question.refresh_from_db()
        assert question.slack_ts == "1800000001.000000"
        assert _sent_pings() == 6

    def test_a_send_outliving_its_claim_still_holds_its_slot_against_a_second_sender(self) -> None:
        _seed_sent_question_pings(5)
        _backlog("Which DB host?")
        stamps = itertools.count(1_800_000_100)

        def post_message(*_args: object, **_kwargs: object) -> dict[str, object]:
            if self.backend.post_message.call_count == 1:
                LoopLease.objects.filter(name="work:owner-question-ping").update(
                    lease_expires_at=timezone.now() - dt.timedelta(seconds=1)
                )
                drain_deferred_questions(user_id="U_ME", backend=self.backend)
            return {"ok": True, "ts": f"{next(stamps)}.000000"}

        self.backend.post_message.side_effect = post_message

        self._poster()

        assert self.backend.post_message.call_count == 1
        assert _sent_pings() == 6

    def test_a_crashed_in_flight_ping_frees_its_slot_and_its_question_is_delivered(self) -> None:
        _seed_sent_question_pings(5)
        question = _backlog("Which DB host?")
        BotPing.objects.create(
            idempotency_key=f"mirror-deferred-question:{question.stable_notify_ref}",
            kind=BotPing.Kind.QUESTION,
            status=BotPing.Status.SENDING,
            audience="owner_question",
            text="Pending question",
        )
        BotPing.objects.filter(status=BotPing.Status.SENDING).update(
            posted_at=F("posted_at") - BotPing.SENDING_STALE_AFTER - dt.timedelta(seconds=1)
        )

        self._poster()

        question.refresh_from_db()
        assert question.slack_ts
        assert _sent_pings() == 6

    def _hold_the_first_send_past_the_stale_bound(self, another_process_sends: Callable[[], object]) -> None:
        stamps = itertools.count(1_800_000_200)

        def post_message(*_args: object, **_kwargs: object) -> dict[str, object]:
            if self.backend.post_message.call_count == 1:
                BotPing.objects.filter(status=BotPing.Status.SENDING).update(
                    posted_at=F("posted_at") - BotPing.SENDING_STALE_AFTER - dt.timedelta(seconds=1)
                )
                LoopLease.objects.filter(name=_QUESTION_PING_SLOT).update(
                    lease_expires_at=timezone.now() - dt.timedelta(seconds=1)
                )
                with _as_another_live_process():
                    another_process_sends()
            return {"ok": True, "ts": f"{next(stamps)}.000000"}

        self.backend.post_message.side_effect = post_message

    def test_a_send_held_past_the_stale_bound_keeps_another_process_from_sending_a_second_message(self) -> None:
        _seed_sent_question_pings(5)
        question = _backlog("Which DB host?")
        self._hold_the_first_send_past_the_stale_bound(
            lambda: drain_deferred_questions(user_id="U_ME", backend=self.backend)
        )

        self._poster()

        question.refresh_from_db()
        assert self.backend.post_message.call_count == 1
        assert question.slack_ts == "1800000200.000000"
        assert _sent_pings() == 6

    def test_a_send_held_past_the_stale_bound_is_not_posted_again_by_a_same_key_recovery(self) -> None:
        _seed_sent_question_pings(5)
        question = _backlog("Which DB host?")
        self._hold_the_first_send_past_the_stale_bound(
            lambda: drain_unmirrored_deferred_questions(user_id="U_ME", backend=self.backend)
        )

        self._poster()

        question.refresh_from_db()
        assert self.backend.post_message.call_count == 1
        assert question.slack_ts == "1800000200.000000"
        assert _sent_pings() == 6

    def test_a_slot_left_by_a_dead_sender_frees_and_its_question_is_delivered(self) -> None:
        _seed_sent_question_pings(5)
        question = _backlog("Which DB host?")
        LoopLease.objects.create(
            name=_QUESTION_PING_SLOT,
            session_id="question-ping:crashed",
            owner_pid=_DEAD_PID,
            owner_pid_namespace=_READER_NS,
            acquired_at=timezone.now() - dt.timedelta(seconds=400),
            lease_expires_at=timezone.now() - dt.timedelta(seconds=280),
        )
        BotPing.objects.create(
            idempotency_key=f"mirror-deferred-question:{question.stable_notify_ref}",
            kind=BotPing.Kind.QUESTION,
            status=BotPing.Status.SENDING,
            audience="owner_question",
            text="Pending question",
        )
        BotPing.objects.filter(status=BotPing.Status.SENDING).update(
            posted_at=F("posted_at") - BotPing.SENDING_STALE_AFTER - dt.timedelta(seconds=1)
        )

        with pinned_reader_namespace(), patch("teatree.utils.singleton.pid_alive", lambda pid: pid != _DEAD_PID):
            self._poster()

        question.refresh_from_db()
        assert question.slack_ts
        assert _sent_pings() == 6

    def test_a_slot_whose_pid_now_names_a_later_process_frees_and_its_question_is_delivered_once(self) -> None:
        _seed_sent_question_pings(5)
        question = _backlog("Which DB host?")
        LoopLease.objects.create(
            name=_QUESTION_PING_SLOT,
            session_id=f"question-ping:{_FOREIGN_PID}:started-before-the-crash:1",
            owner_pid=_FOREIGN_PID,
            owner_pid_namespace=_READER_NS,
            acquired_at=timezone.now() - dt.timedelta(seconds=400),
            lease_expires_at=timezone.now() - dt.timedelta(seconds=280),
        )
        BotPing.objects.create(
            idempotency_key=f"mirror-deferred-question:{question.stable_notify_ref}",
            kind=BotPing.Kind.QUESTION,
            status=BotPing.Status.SENDING,
            audience="owner_question",
            text="Pending question",
        )
        BotPing.objects.filter(status=BotPing.Status.SENDING).update(
            posted_at=F("posted_at") - BotPing.SENDING_STALE_AFTER - dt.timedelta(seconds=1)
        )
        own_pid = os.getpid()

        with (
            pinned_reader_namespace(),
            patch("teatree.utils.singleton.pid_alive", lambda pid: pid in {own_pid, _FOREIGN_PID}),
            patch.object(
                drains_module,
                "_process_start_token",
                lambda pid: "started-after-the-crash" if pid == _FOREIGN_PID else "",
                create=True,
            ),
        ):
            self._poster()

        question.refresh_from_db()
        assert self.backend.post_message.call_count == 1
        assert question.slack_ts
        assert _sent_pings() == 6

    def test_a_reader_in_another_pid_namespace_leaves_a_live_holder_its_final_slot(self) -> None:
        _seed_sent_question_pings(5)
        question = _backlog("Which DB host?")
        holder = f"question-ping:{_FOREIGN_PID}:worker-started:1"
        LoopLease.objects.create(
            name=_QUESTION_PING_SLOT,
            session_id=holder,
            owner_pid=_FOREIGN_PID,
            owner_pid_namespace=_OTHER_NS,
            acquired_at=timezone.now(),
            lease_expires_at=timezone.now() + dt.timedelta(seconds=120),
        )
        own_pid = os.getpid()

        with (
            pinned_reader_namespace(),
            patch("teatree.utils.singleton.pid_alive", lambda pid: pid in {own_pid, _FOREIGN_PID}),
            patch.object(
                drains_module,
                "_process_start_token",
                lambda pid: "an-unrelated-process-in-this-container" if pid == _FOREIGN_PID else "",
                create=True,
            ),
        ):
            self._poster()

        question.refresh_from_db()
        assert self.backend.post_message.call_count == 0
        assert not question.slack_ts
        assert LoopLease.objects.get(name=_QUESTION_PING_SLOT).session_id == holder

    def _expired_holder_in_an_unrecorded_namespace(self) -> str:
        holder = f"question-ping:{_FOREIGN_PID}::1"
        LoopLease.objects.create(
            name=_QUESTION_PING_SLOT,
            session_id=holder,
            owner_pid=_FOREIGN_PID,
            owner_pid_namespace="",
            acquired_at=timezone.now() - dt.timedelta(seconds=400),
            lease_expires_at=timezone.now() - dt.timedelta(seconds=280),
        )
        return holder

    def test_an_expired_holder_whose_pid_now_names_a_live_process_frees_the_slot_for_one_send(self) -> None:
        _seed_sent_question_pings(5)
        _backlog("Which DB host?")
        _backlog("Which queue?")
        self._expired_holder_in_an_unrecorded_namespace()
        own_pid = os.getpid()

        with patch("teatree.utils.singleton.pid_alive", lambda pid: pid in {own_pid, _FOREIGN_PID}):
            self._poster()

        assert self.backend.post_message.call_count == 1
        assert _sent_pings() == 6

    def _an_unfinalized_send(self, *, claimed_ago: dt.timedelta) -> None:
        BotPing.objects.create(
            idempotency_key="mirror-deferred-question:still-sending",
            kind=BotPing.Kind.QUESTION,
            status=BotPing.Status.SENDING,
            audience="owner_question",
            text="Pending question",
        )
        BotPing.objects.filter(status=BotPing.Status.SENDING).update(posted_at=F("posted_at") - claimed_ago)

    def test_an_expired_holder_with_a_send_held_past_the_stale_bound_keeps_the_slot(self) -> None:
        _seed_sent_question_pings(5)
        _backlog("Which DB host?")
        holder = self._expired_holder_in_an_unrecorded_namespace()
        self._an_unfinalized_send(claimed_ago=BotPing.SENDING_STALE_AFTER + dt.timedelta(seconds=1))
        own_pid = os.getpid()

        with patch("teatree.utils.singleton.pid_alive", lambda pid: pid in {own_pid, _FOREIGN_PID}):
            self._poster()

        assert self.backend.post_message.call_count == 0
        assert LoopLease.objects.get(name=_QUESTION_PING_SLOT).session_id == holder

    def test_an_expired_holder_whose_unfinalized_send_was_claimed_over_an_hour_ago_frees_the_slot(self) -> None:
        _seed_sent_question_pings(5)
        _backlog("Which DB host?")
        self._expired_holder_in_an_unrecorded_namespace()
        self._an_unfinalized_send(claimed_ago=dt.timedelta(hours=1, seconds=1))
        own_pid = os.getpid()

        with patch("teatree.utils.singleton.pid_alive", lambda pid: pid in {own_pid, _FOREIGN_PID}):
            self._poster()

        assert self.backend.post_message.call_count == 1
        assert _sent_pings() == 6


class TestTheHourlyCeilingHoldsAcrossProcesses:
    """A tick worker and a manual resurface run in separate processes, so no in-process lock can pace them."""

    def test_two_processes_racing_for_the_last_slot_send_one_ping(self, schema_guard_alias: SchemaGuardAlias) -> None:
        alias = schema_guard_alias.register_current()
        connections.databases[alias]["OPTIONS"].update(SQLITE_WRITE_SERIALIZATION_OPTIONS)
        connections[alias].close()
        _seed_sent_question_pings(5)
        _backlog("Which DB host?")
        _backlog("Which region?")
        backend = _backend_holding_each_send_open(senders=2)
        drains = (drain_unmirrored_deferred_questions, drain_deferred_questions)
        start = threading.Barrier(len(drains), timeout=10)

        real_pid = os.getpid()
        pids: dict[int, int] = {}

        def run_one_process(index: int) -> tuple[int, int]:
            pids[threading.get_ident()] = _FOREIGN_PID + index
            try:
                start.wait()
                return drains[index](user_id="U_ME", backend=backend)
            finally:
                close_thread_db_connections()

        with (
            patch.object(notify_module, "messaging_from_overlay", return_value=backend),
            patch.object(drains_module, "_QUESTION_PING_THREAD_GUARD", _NoInProcessGuard(), create=True),
            patch("os.getpid", lambda: pids.get(threading.get_ident(), real_pid)),
            patch("teatree.utils.singleton.pid_alive", lambda _pid: True),
        ):
            run_racing_threads(run_one_process, len(drains))

        assert _sent_pings() == 6
