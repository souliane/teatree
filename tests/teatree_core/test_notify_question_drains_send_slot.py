"""Question drains serialize sends across threads and processes."""

import contextlib
import datetime as dt
import itertools
import os
import threading
from collections.abc import Callable, Generator
from unittest.mock import MagicMock, patch

import pytest
from django.db.models import F
from django.test import TestCase
from django.utils import timezone

from teatree.core import notify as notify_module
from teatree.core import notify_question_drains as drains_module
from teatree.core.models import BotPing, DeferredQuestion, LoopLease
from teatree.core.notify_question_drains import drain_deferred_questions, drain_unmirrored_deferred_questions
from teatree.loop.scanners.deferred_question_poster import DeferredQuestionPosterScanner
from tests._owner_channel import OWNER_DECISION
from tests.db_alias import wal_control_db
from tests.teatree_core.test_claim_liveness import _OTHER_NS, _READER_NS, pinned_reader_namespace

pytestmark = pytest.mark.usefixtures(wal_control_db.__name__)

_CHANNEL = "D-USER"
_QUESTION_PING_SLOT = "work:owner-question-ping"
_FOREIGN_PID = 4_000_000
_DEAD_PID = 4_100_000


@contextlib.contextmanager
def _as_another_live_process() -> Generator[None]:
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
    channel = _CHANNEL if slack_ts else ""
    row = DeferredQuestion.record(question, session_id="s", slack_channel=channel, slack_ts=slack_ts, **OWNER_DECISION)
    DeferredQuestion.objects.filter(pk=row.pk).update(created_at=timezone.now() - dt.timedelta(days=age_days))
    row.refresh_from_db()
    return row


def _seed_sent_question_pings(count: int) -> None:
    for index in range(count):
        BotPing.objects.create(
            idempotency_key=f"reask:seeded-{index}:1",
            kind=BotPing.Kind.QUESTION,
            status=BotPing.Status.SENT,
            audience="owner_question",
            text="Still waiting on this one",
        )


def _sent_pings() -> int:
    return BotPing.objects.filter(status=BotPing.Status.SENT).count()


@contextlib.contextmanager
def _sibling_holds(guard: threading.Lock) -> Generator[threading.Event]:
    acquired = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with guard:
            acquired.set()
            release.wait(timeout=30)

    sibling = threading.Thread(target=hold)
    sibling.start()
    assert acquired.wait(timeout=5)
    try:
        yield release
    finally:
        release.set()
        sibling.join(timeout=2)
        assert not sibling.is_alive()


class _ReleaseSiblingOnAcquire:
    def __init__(self, guard: threading.Lock, release: threading.Event) -> None:
        self.guard = guard
        self.release_sibling = release

    def acquire(self, *, blocking: bool = True, timeout: float = -1) -> bool:
        release = threading.Timer(0.2, self.release_sibling.set)
        release.start()
        try:
            return self.guard.acquire(blocking=blocking, timeout=timeout)
        finally:
            release.join()

    def release(self) -> None:
        self.guard.release()


class TestTheQuestionDrainsShareASendSlot(TestCase):
    def setUp(self) -> None:
        self.backend = _backend()
        messaging = patch.object(notify_module, "messaging_from_overlay", return_value=self.backend)
        messaging.start()
        self.addCleanup(messaging.stop)

    def _poster(self) -> None:
        DeferredQuestionPosterScanner(backend=self.backend, user_id="U_ME").scan()

    def test_a_sibling_finishing_mid_send_lets_each_drain_post(self) -> None:
        for name, drain, slack_ts, expected in (
            ("poster", drain_unmirrored_deferred_questions, "", (1, 1)),
            ("manual resurface", drain_deferred_questions, "", (1, 1)),
            ("reask", drains_module.reask_escalated_questions, "100.0", (1, 1)),
        ):
            with self.subTest(drain=name):
                DeferredQuestion.objects.all().delete()
                BotPing.objects.all().delete()
                self.backend.post_message.reset_mock()
                question = _backlog("Which DB host?", slack_ts=slack_ts)
                guard = threading.Lock()
                with (
                    _sibling_holds(guard) as release,
                    patch.object(
                        drains_module, "_QUESTION_PING_THREAD_GUARD", _ReleaseSiblingOnAcquire(guard, release)
                    ),
                ):
                    result = drain(user_id="U_ME", backend=self.backend)

                assert result == expected
                self.backend.post_message.assert_called_once()
                assert not guard.locked()
                question.refresh_from_db()
                assert question.slack_ts

    def test_a_sibling_outliving_the_wait_keeps_each_drain_pending_and_logs_why(self) -> None:
        for name, drain, slack_ts, expected in (
            ("poster", drain_unmirrored_deferred_questions, "", (0, 1)),
            ("manual resurface", drain_deferred_questions, "", (0, 1)),
            ("reask", drains_module.reask_escalated_questions, "100.0", (0, 1)),
        ):
            with self.subTest(drain=name):
                DeferredQuestion.objects.all().delete()
                BotPing.objects.all().delete()
                self.backend.post_message.reset_mock()
                question = _backlog("Which DB host?", slack_ts=slack_ts)
                guard = threading.Lock()
                with (
                    _sibling_holds(guard) as release,
                    patch.object(
                        drains_module, "_QUESTION_PING_THREAD_GUARD", _ReleaseSiblingOnAcquire(guard, release)
                    ),
                    patch.object(drains_module, "_QUESTION_PING_THREAD_WAIT_SECONDS", 0.03),
                    self.assertLogs(drains_module.logger, level="INFO") as captured,
                ):
                    result = drain(user_id="U_ME", backend=self.backend)

                assert result == expected
                self.backend.post_message.assert_not_called()
                assert not BotPing.objects.exists()
                assert any(
                    "Another drain in this process still holds the owner question-ping slot after 0.03s; "
                    "this ping waits for the next pass" in line
                    for line in captured.output
                )
                question.refresh_from_db()
                assert question.slack_ts == slack_ts

    def test_an_uncontended_drain_posts_with_the_bounded_guard(self) -> None:
        question = _backlog("Which DB host?")
        guard = threading.Lock()
        with patch.object(drains_module, "_QUESTION_PING_THREAD_GUARD", guard):
            self._poster()

        question.refresh_from_db()
        assert question.slack_ts
        assert guard.acquire(blocking=False)
        guard.release()

    def test_forty_one_unposted_questions_all_send_across_passes_in_one_hour(self) -> None:
        for index in range(41):
            _backlog(f"Unposted {index}?")
        for _ in range(14):
            self._poster()

        assert DeferredQuestion.unmirrored_pending().count() == 0
        assert self.backend.post_message.call_count == 41

    def test_every_first_card_posts_in_one_pass(self) -> None:
        for index in range(8):
            _backlog(f"Unposted {index}?")

        self._poster()

        assert DeferredQuestion.unmirrored_pending().count() == 0
        assert self.backend.post_message.call_count == 8

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

    def test_an_expired_holder_whose_pid_now_names_a_live_process_frees_the_slot_for_first_cards(self) -> None:
        _seed_sent_question_pings(5)
        _backlog("Which DB host?")
        _backlog("Which queue?")
        self._expired_holder_in_an_unrecorded_namespace()
        own_pid = os.getpid()

        with patch("teatree.utils.singleton.pid_alive", lambda pid: pid in {own_pid, _FOREIGN_PID}):
            self._poster()

        assert self.backend.post_message.call_count == 2
        assert _sent_pings() == 7

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
