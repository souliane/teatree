"""The recurring open-question digest — one message per interval, same DM thread.

Directive #36: resurface the open questions as often as necessary until the
owner answers, in the SAME Slack thread and with the message count as low as
possible. The per-question first-post drains are single-shot (their idempotency
key is per question), so nothing recurred; this digest is the recurring half and
it collapses the whole backlog into ONE message per interval.
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
    def test_digest_lists_every_pending_question_id_in_one_message(self) -> None:
        first = DeferredQuestion.record("Unify the worktree output root?")
        second = DeferredQuestion.record("Which merge target for the coding phase?")

        text = format_backlog_digest([first, second])

        assert f"#{first.pk}" in text
        assert f"#{second.pk}" in text
        assert "Unify the worktree output root?" in text
        assert "2 open question" in text

    def test_digest_caps_the_list_and_names_the_remainder(self) -> None:
        rows = [DeferredQuestion.record(f"Question {i}") for i in range(14)]

        text = format_backlog_digest(rows)

        assert "+4 more" in text
        assert f"#{rows[-1].pk}" not in text

    def test_digest_tells_the_owner_to_reply_with_the_question_id(self) -> None:
        row = DeferredQuestion.record("Merge it?")

        text = format_backlog_digest([row])

        assert "reply" in text.lower()
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

    def test_an_escalated_row_survives_the_list_cap(self) -> None:
        # The cap is what makes the stamp worth rendering: in a 14-deep backlog the row
        # that has waited longest without an answer is exactly the one the cap would drop.
        rows = [DeferredQuestion.record(f"Question {i}") for i in range(14)]
        assert rows[-1].mark_escalated("pending past the ceiling")

        text = format_backlog_digest(rows)

        assert f"#{rows[-1].pk}" in text


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
