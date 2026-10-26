"""The recurring open-question digest — one COUNT per interval, same DM thread.

Directive #36: resurface the open questions as often as necessary until the
owner answers, in the SAME Slack thread and with the message count as low as
possible. The per-question first-post drains are single-shot (their idempotency
key is per question), so nothing recurred; this digest is the recurring half and
it collapses the whole backlog into ONE message per interval.

What the digest may NOT do is carry the questions themselves. A reply under it is
stamped with the digest's thread ts, which joins no question, and at a backlog
deeper than one the sole-live-question rung refuses to guess — so every question
named here was a question asked where its answer could not land. The detail moved
to ``reask_escalated_questions``, into each question's own thread; the digest keeps
the counts that frame it.
"""

import datetime as dt
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import DeferredQuestion
from teatree.core.notify_question_drains import RESURFACE_INTERVAL_HOURS, resurface_question_backlog
from teatree.core.owner_question_message import digest_text
from teatree.core.worktree.checkout_attest import CheckoutAttestation, ask_owner_to_attest
from teatree.core.worktree.checkout_state import CheckoutState
from tests._owner_channel import OWNER_DECISION
from tests.factories import TaskFactory, WorktreeFactory


class TestBacklogDigestText(TestCase):
    def test_digest_counts_the_backlog_rather_than_listing_it(self) -> None:
        first = DeferredQuestion.record("Unify the worktree output root?", **OWNER_DECISION)
        second = DeferredQuestion.record("Which merge target for the coding phase?", **OWNER_DECISION)

        text = digest_text([first, second], now=timezone.now())

        assert text.startswith("2 decisions are waiting for you")
        assert f"#{first.pk}" not in text, "a question named in the digest cannot be answered from it"
        assert f"#{second.pk}" not in text
        assert "Unify the worktree output root?" not in text

    def test_digest_stays_one_message_however_deep_the_backlog(self) -> None:
        rows = [DeferredQuestion.record(f"Widget {i}?", **OWNER_DECISION) for i in range(14)]

        text = digest_text(rows, now=timezone.now())

        assert text.startswith("14 decisions are waiting for you")
        assert len(text.splitlines()) == 1, "the digest grew a per-question list again"

    def test_digest_reports_how_long_the_oldest_has_waited(self) -> None:
        old = DeferredQuestion.record("Merge it?", **OWNER_DECISION)
        DeferredQuestion.objects.filter(pk=old.pk).update(created_at=timezone.now() - dt.timedelta(days=34))
        old.refresh_from_db()
        old.slack_ts = str((timezone.now() - dt.timedelta(days=34)).timestamp())

        assert "the oldest was asked 34 days ago" in digest_text([old, old], now=timezone.now())

    def test_digest_names_no_command_and_no_id(self) -> None:
        row = DeferredQuestion.record("Merge it?", **OWNER_DECISION)

        text = digest_text([row], now=timezone.now())

        assert "t3 " not in text
        assert "#" not in text
        assert "deferred" not in text.lower()


class TestResurfaceQuestionBacklog(TestCase):
    def test_empty_backlog_posts_nothing(self) -> None:
        with patch("teatree.core.notify_question_drains.notify_user") as notify:
            posted, pending = resurface_question_backlog()

        notify.assert_not_called()
        assert (posted, pending) == (False, 0)

    def test_internal_rows_never_reach_the_owner_digest(self) -> None:
        DeferredQuestion.record("I lack the shell tool to proceed.")

        with patch("teatree.core.notify_question_drains.notify_user") as notify:
            posted, pending = resurface_question_backlog()

        notify.assert_not_called()
        assert (posted, pending) == (False, 0)

    def test_one_digest_covers_the_whole_backlog(self) -> None:
        DeferredQuestion.record("First?", **OWNER_DECISION)
        DeferredQuestion.record("Second?", **OWNER_DECISION)
        DeferredQuestion.record("Third?", **OWNER_DECISION)
        DeferredQuestion.objects.update(created_at=timezone.now() - dt.timedelta(hours=2))

        with patch("teatree.core.notify_question_drains.notify_user", return_value=True) as notify:
            posted, pending = resurface_question_backlog()

        assert notify.call_count == 1
        assert (posted, pending) == (True, 3)

    def test_fresh_posted_cards_do_not_trigger_a_digest(self) -> None:
        now = timezone.now()
        DeferredQuestion.record("Merge it?", slack_ts=str(now.timestamp()), slack_channel="D1", **OWNER_DECISION)

        with patch("teatree.core.notify_question_drains.notify_user") as notify:
            result = resurface_question_backlog(now=now)

        assert result == (False, 1)
        notify.assert_not_called()

    def test_fresh_unposted_question_does_not_trigger_a_digest(self) -> None:
        DeferredQuestion.record("Which option?", **OWNER_DECISION)

        with patch("teatree.core.notify_question_drains.notify_user") as notify:
            result = resurface_question_backlog(now=timezone.now())

        assert result == (False, 1)
        notify.assert_not_called()

    def test_one_old_card_keeps_a_mixed_age_digest_due(self) -> None:
        old = DeferredQuestion.record("Old decision?", **OWNER_DECISION)
        DeferredQuestion.objects.filter(pk=old.pk).update(created_at=timezone.now() - dt.timedelta(hours=2))
        DeferredQuestion.record("New decision?", **OWNER_DECISION)

        with (
            patch("teatree.core.notify_question_drains.owner_quiet_now", return_value=False),
            patch("teatree.core.notify_question_drains.notify_user", return_value=True) as notify,
        ):
            result = resurface_question_backlog(now=timezone.now())

        assert result == (True, 2)
        notify.assert_called_once()

    def test_unposted_attest_card_under_hold_does_not_trigger_a_digest(self) -> None:
        task = TaskFactory()
        worktree = WorktreeFactory(ticket=task.ticket)
        card = ask_owner_to_attest(
            task, CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)
        )
        assert card is not None

        with patch("teatree.core.notify_question_drains.notify_user") as notify:
            result = resurface_question_backlog()

        assert result == (False, 0)
        notify.assert_not_called()

    def test_the_interval_bucket_is_the_idempotency_key(self) -> None:
        DeferredQuestion.record("Merge it?", **OWNER_DECISION)
        now = dt.datetime(2026, 10, 10, 12, tzinfo=dt.UTC)
        DeferredQuestion.objects.update(created_at=now - dt.timedelta(hours=2))

        with patch("teatree.core.notify_question_drains.notify_user", return_value=True) as notify:
            resurface_question_backlog(now=now)
            key_first = notify.call_args.kwargs["idempotency_key"]
            resurface_question_backlog(now=now + dt.timedelta(minutes=5))
            key_same_bucket = notify.call_args.kwargs["idempotency_key"]
            resurface_question_backlog(now=now + dt.timedelta(hours=RESURFACE_INTERVAL_HOURS))
            key_next_bucket = notify.call_args.kwargs["idempotency_key"]

        # Same bucket → the BotPing ledger collapses the repeat; a new bucket is a new nag.
        assert key_first == key_same_bucket
        assert key_next_bucket != key_first

    def test_the_interval_bucket_rotates_at_utc_midnight(self) -> None:
        DeferredQuestion.record("Merge it?", **OWNER_DECISION)
        before_midnight = dt.datetime(2026, 10, 10, 23, 58, tzinfo=dt.UTC)
        DeferredQuestion.objects.update(created_at=before_midnight - dt.timedelta(hours=2))

        with patch("teatree.core.notify_question_drains.notify_user", return_value=True) as notify:
            resurface_question_backlog(now=before_midnight)
            key_before_midnight = notify.call_args.kwargs["idempotency_key"]
            resurface_question_backlog(now=before_midnight + dt.timedelta(minutes=5))
            key_after_midnight = notify.call_args.kwargs["idempotency_key"]

        assert key_before_midnight != key_after_midnight

    def test_a_failed_delivery_reports_not_posted(self) -> None:
        DeferredQuestion.record("Merge it?", **OWNER_DECISION)

        with patch("teatree.core.notify_question_drains.notify_user", return_value=False):
            posted, pending = resurface_question_backlog()

        assert (posted, pending) == (False, 1)
