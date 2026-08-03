"""The DeferredQuestion resurface DM must be Slack-reply-only, never a host CLI.

The owner reads Slack DMs and has NO host-CLI access — every interaction is in
Slack. The resurface/mirror message the drains post therefore must NOT tell the
owner to run ``t3 <overlay> questions answer …``; the owner just replies in the
thread and the reply scanner binds the answer.
"""

import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import BotPing, DeferredQuestion
from teatree.core.notify_question_drains import _resurface_text, drain_deferred_questions


class TestResurfaceMessageHasNoHostCli(TestCase):
    def test_message_carries_no_t3_cli_instruction(self) -> None:
        row = DeferredQuestion.record(question="Should I merge PR #7?", session_id="s1")

        text = _resurface_text(row)

        # No host-CLI instruction of any kind — the owner cannot run one.
        assert "t3 " not in text
        assert "questions answer" not in text
        assert "Answer with" not in text
        # It DOES tell the owner to reply in the thread instead.
        assert "reply" in text.lower()
        assert "thread" in text.lower()

    def test_message_still_renders_question_and_options(self) -> None:
        row = DeferredQuestion.record(
            question="Pick a rollout",
            options_json=json.dumps([{"label": "canary", "description": "10% first"}]),
            session_id="s2",
        )

        text = _resurface_text(row)

        assert "Pick a rollout" in text
        assert "canary" in text
        assert "t3 " not in text


class TestDrainExcludesInternalAudience(TestCase):
    """An INTERNAL row (an agent tool-lack self-report) never joins the owner DM batch."""

    def test_internal_only_backlog_drains_nothing(self) -> None:
        DeferredQuestion.record(
            "This session lacks any shell/write tool to run record_candidate.",
            audience=DeferredQuestion.Audience.INTERNAL,
        )
        with patch("teatree.core.notify_question_drains.notify_user") as notify:
            delivered, total = drain_deferred_questions()
        # The INTERNAL row is filtered before any egress — notify_user is never called.
        notify.assert_not_called()
        assert (delivered, total) == (0, 0)

    def test_owner_row_drains_but_internal_peer_is_excluded(self) -> None:
        owner = DeferredQuestion.record("Should I merge PR #7?")
        DeferredQuestion.record(
            "I run shell-denied and cannot file the issue.",
            audience=DeferredQuestion.Audience.INTERNAL,
        )
        with patch("teatree.core.notify_question_drains.notify_user", return_value=True) as notify:
            delivered, total = drain_deferred_questions()
        # Exactly one egress — the owner row — and the total counts only it.
        assert notify.call_count == 1
        assert owner.question in notify.call_args.args[0]
        assert (delivered, total) == (1, 1)


class TestDrainAdvancesPastAlreadyDeliveredRows(TestCase):
    """The per-call cap must bound NEW deliveries, never re-select a delivered head (#4064).

    ``pending()`` is oldest-first and the slice was taken straight off it, so the same
    three oldest rows filled the window on every call, deduped to no-ops, and every row
    behind them was unreachable — on a tick, on an away->present transition, or on a
    manual resurface. The module's own comment promised the opposite ("the remainder is
    re-read on the next tick"), which is the behaviour these pin.
    """

    def _delivered(self, row: DeferredQuestion) -> None:
        """Stand in for an earlier drain that already DM'd *row*."""
        BotPing.objects.create(
            idempotency_key=f"resurface-deferred-question:{row.stable_notify_ref}",
            kind=BotPing.Kind.QUESTION,
            status=BotPing.Status.SENT,
            text="already sent",
        )

    def test_a_delivered_head_does_not_block_the_rest_of_the_backlog(self) -> None:
        rows = [DeferredQuestion.record(f"Q{i}") for i in range(5)]
        for row in rows[:3]:
            self._delivered(row)

        with patch("teatree.core.notify_question_drains.notify_user", return_value=True) as notify:
            delivered, _total = drain_deferred_questions()

        posted = " ".join(str(call.args[0]) for call in notify.call_args_list)
        assert "Q3" in posted, "the window must advance past the delivered head"
        assert "Q4" in posted
        assert "Q0" not in posted, "an already-delivered row must not re-occupy the cap"
        assert delivered == 2

    def test_total_reports_the_backlog_not_the_capped_slice(self) -> None:
        for i in range(5):
            DeferredQuestion.record(f"Q{i}")

        with patch("teatree.core.notify_question_drains.notify_user", return_value=True):
            _delivered, total = drain_deferred_questions()

        assert total == 5, "reporting the slice as the denominator hides the backlog"


class TestOnlyASentPingCountsAsDelivered(TestCase):
    """A FAILED or NOOP ping is a delivery that did not land, so it must not skip the row.

    `notify_user` treats only SENT as already-delivered and re-delivers FAILED / NOOP, so a
    status-blind read-back skips such a question permanently — worse than the head-blocking it
    replaces, where a transient failure self-healed on the next call.
    """

    def _ping(self, row: DeferredQuestion, status: str) -> None:
        BotPing.objects.create(
            idempotency_key=f"resurface-deferred-question:{row.stable_notify_ref}",
            kind=BotPing.Kind.QUESTION,
            status=status,
            text="attempted",
        )

    def test_a_failed_ping_does_not_mark_the_question_delivered(self) -> None:
        row = DeferredQuestion.record("Should I merge PR #7?")
        self._ping(row, BotPing.Status.FAILED)

        with patch("teatree.core.notify_question_drains.notify_user", return_value=True) as notify:
            delivered, total = drain_deferred_questions()

        assert delivered == 1
        assert total == 1
        assert row.question in str(notify.call_args.args[0])

    def test_a_noop_ping_does_not_mark_the_question_delivered(self) -> None:
        row = DeferredQuestion.record("Pick a rollout")
        self._ping(row, BotPing.Status.NOOP)

        with patch("teatree.core.notify_question_drains.notify_user", return_value=True) as notify:
            delivered, _total = drain_deferred_questions()

        assert delivered == 1
        assert row.question in str(notify.call_args.args[0])


class TestARowTheSendPathWillNotRedeliverFreesItsCapSlot(TestCase):
    """A ping whose next claim stands down must be skipped by the read-back too (#4064).

    ``BotPing.claim_delivery`` returns ``IN_FLIGHT`` — no DM — for SENT_UNVERIFIED, EXPIRED,
    LOGGED and a FRESH SENDING. A read-back scoped to SENT leaves such a row in the selection
    window, where it is neither skipped by the selection nor re-delivered by the send, so it
    permanently occupies one of the three cap slots and the drain stays head-blocked. The
    predicate is "will the send actually re-deliver this?", not "is it SENT?".
    """

    def _ping(self, row: DeferredQuestion, status: str, *, age: timedelta = timedelta(0)) -> None:
        BotPing.objects.create(
            idempotency_key=f"resurface-deferred-question:{row.stable_notify_ref}",
            kind=BotPing.Kind.QUESTION,
            status=status,
            text="attempted",
            posted_at=timezone.now() - age,
        )

    def test_a_stood_down_head_does_not_re_occupy_a_cap_slot(self) -> None:
        for status in (BotPing.Status.SENT_UNVERIFIED, BotPing.Status.EXPIRED, BotPing.Status.SENDING):
            # ``str(status)``: xdist serialises subTest kwargs across the worker channel and
            # cannot dump a TextChoices member, which fails the node under `-n auto` only.
            with self.subTest(status=str(status)):
                DeferredQuestion.objects.all().delete()
                BotPing.objects.all().delete()
                rows = [DeferredQuestion.record(f"Q{i}") for i in range(5)]
                self._ping(rows[0], status)

                with patch("teatree.core.notify_question_drains.notify_user", return_value=True) as notify:
                    delivered, total = drain_deferred_questions()

                posted = " ".join(str(call.args[0]) for call in notify.call_args_list)
                assert "Q0" not in posted, "a row the send stands down on must not hold a cap slot"
                assert [q for q in ("Q1", "Q2", "Q3") if q in posted] == ["Q1", "Q2", "Q3"]
                assert (delivered, total) == (3, 5)

    def test_a_stale_sending_claim_stays_redeliverable(self) -> None:
        row = DeferredQuestion.record("Should I merge PR #7?")
        self._ping(row, BotPing.Status.SENDING, age=BotPing.SENDING_STALE_AFTER + timedelta(seconds=1))

        with patch("teatree.core.notify_question_drains.notify_user", return_value=True) as notify:
            delivered, total = drain_deferred_questions()

        # A crashed claim is recoverable — claim_delivery replaces it and the DM goes out.
        assert (delivered, total) == (1, 1)
        assert row.question in str(notify.call_args.args[0])
