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
from teatree.core.notify_question_drains import (
    RESURFACE_INTERVAL_HOURS,
    format_backlog_digest,
    resurface_question_backlog,
)


class TestBacklogDigestText(TestCase):
    def test_digest_counts_the_backlog_rather_than_listing_it(self) -> None:
        first = DeferredQuestion.record("Unify the worktree output root?")
        second = DeferredQuestion.record("Which merge target for the coding phase?")

        text = format_backlog_digest([first, second])

        assert "2 open questions need decisions" in text
        assert f"#{first.pk}" not in text, "a question named in the digest cannot be answered from it"
        assert f"#{second.pk}" not in text
        assert "Unify the worktree output root?" not in text

    def test_digest_stays_one_message_however_deep_the_backlog(self) -> None:
        rows = [DeferredQuestion.record(f"Question {i}") for i in range(14)]

        text = format_backlog_digest(rows)

        assert "14 open questions need decisions" in text
        assert len(text.splitlines()) <= 3, "the digest grew a per-question list again"

    def test_digest_reports_how_long_the_oldest_has_waited(self) -> None:
        old = DeferredQuestion.record("Merge it?")
        DeferredQuestion.objects.filter(pk=old.pk).update(created_at=timezone.now() - dt.timedelta(days=34))
        old.refresh_from_db()

        assert "oldest is 34d" in format_backlog_digest([old])

    def test_digest_tells_the_owner_where_an_answer_binds(self) -> None:
        row = DeferredQuestion.record("Merge it?")

        text = format_backlog_digest([row])

        assert "reply" in text.lower()
        assert "`#<id> <your answer>`" in text, "the one form that binds from outside a question thread"
        assert "t3 " not in text


class TestEscalationIsVisibleInTheDigest(TestCase):
    """The age backstop's stamp has to CHANGE something the owner reads (#4178).

    ``escalated_at``/``escalation_count`` are written by the drain's backstop stage. A
    stamp nothing renders is write-only: "a KEEP is not a licence to sit forever" would
    then be satisfied by doing nothing at all. These are the positive criteria — only a
    rendered escalation can pass them.
    """

    def test_an_escalated_row_reads_differently_from_the_same_row_unescalated(self) -> None:
        row = DeferredQuestion.record("Merge it?")
        before = format_backlog_digest([row])

        assert row.mark_escalated("pending past the 7d ceiling with no resolution")

        assert format_backlog_digest([row]) != before

    def test_the_header_counts_the_escalated_rows(self) -> None:
        escalated = DeferredQuestion.record("Merge it?")
        DeferredQuestion.record("And this one?")
        assert escalated.mark_escalated("pending past the ceiling")

        text = format_backlog_digest([escalated, DeferredQuestion.objects.exclude(pk=escalated.pk).get()])

        assert "1 past the age ceiling" in text

    def test_the_escalated_count_survives_a_deep_backlog(self) -> None:
        # The digest names no row at any depth, so what has to survive is the COUNT: a
        # 14-deep backlog with one row past the ceiling still says so.
        rows = [DeferredQuestion.record(f"Question {i}") for i in range(14)]
        assert rows[-1].mark_escalated("pending past the ceiling")

        assert "1 past the age ceiling" in format_backlog_digest(rows)


class TestResurfaceQuestionBacklog(TestCase):
    def test_empty_backlog_posts_nothing(self) -> None:
        with patch("teatree.core.notify_question_drains.notify_user") as notify:
            posted, pending = resurface_question_backlog()

        notify.assert_not_called()
        assert (posted, pending) == (False, 0)

    def test_internal_rows_never_reach_the_owner_digest(self) -> None:
        DeferredQuestion.record("I lack the shell tool to proceed.", audience=DeferredQuestion.Audience.INTERNAL)

        with patch("teatree.core.notify_question_drains.notify_user") as notify:
            posted, pending = resurface_question_backlog()

        notify.assert_not_called()
        assert (posted, pending) == (False, 0)

    def test_one_digest_covers_the_whole_backlog(self) -> None:
        DeferredQuestion.record("First?")
        DeferredQuestion.record("Second?")
        DeferredQuestion.record("Third?")

        with patch("teatree.core.notify_question_drains.notify_user", return_value=True) as notify:
            posted, pending = resurface_question_backlog()

        assert notify.call_count == 1
        assert (posted, pending) == (True, 3)

    def test_the_interval_bucket_is_the_idempotency_key(self) -> None:
        DeferredQuestion.record("Merge it?")
        now = timezone.now()

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

    def test_a_failed_delivery_reports_not_posted(self) -> None:
        DeferredQuestion.record("Merge it?")

        with patch("teatree.core.notify_question_drains.notify_user", return_value=False):
            posted, pending = resurface_question_backlog()

        assert (posted, pending) == (False, 1)
