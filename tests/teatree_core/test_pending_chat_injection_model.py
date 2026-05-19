"""Tests for :class:`PendingChatInjection` — the Slack-inbound queue (#1014).

Issue #1063 adds the ``answered_at`` gate; tests for the ``is_question``
heuristic live in a separate file (``test_pending_chat_injection_is_question.py``).
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from teatree.core.models import PendingChatInjection

pytestmark = pytest.mark.django_db


class TestRecordIdempotency:
    """The scanner over-polls; ``record`` must be safe to call twice on the same ``ts``."""

    def test_same_ts_recorded_twice_yields_one_row(self) -> None:
        first = PendingChatInjection.record(
            channel="D0DEMOTEAM1", slack_ts="1700000000.0001", text="hello", user_id="U0DEMOUSER1"
        )
        second = PendingChatInjection.record(
            channel="D0DEMOTEAM1", slack_ts="1700000000.0001", text="hello", user_id="U0DEMOUSER1"
        )

        assert first is not None
        assert second is None
        assert PendingChatInjection.objects.count() == 1

    def test_distinct_ts_yields_distinct_rows(self) -> None:
        PendingChatInjection.record(channel="D0DEMOTEAM1", slack_ts="1.0", text="a")
        PendingChatInjection.record(channel="D0DEMOTEAM1", slack_ts="2.0", text="b")

        assert PendingChatInjection.objects.count() == 2

    def test_different_overlays_with_same_ts_are_distinct(self) -> None:
        PendingChatInjection.record(channel="C1", slack_ts="1.0", text="a", overlay="ovA")
        PendingChatInjection.record(channel="C2", slack_ts="1.0", text="b", overlay="ovB")

        assert PendingChatInjection.objects.count() == 2

    def test_empty_text_is_rejected(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="1.0", text="   ")

        assert row is None
        assert PendingChatInjection.objects.count() == 0

    def test_empty_ts_is_rejected(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="", text="hi")

        assert row is None
        assert PendingChatInjection.objects.count() == 0

    def test_empty_channel_is_rejected(self) -> None:
        row = PendingChatInjection.record(channel="", slack_ts="1.0", text="hi")

        assert row is None
        assert PendingChatInjection.objects.count() == 0


class TestPendingQuery:
    def test_pending_excludes_consumed_rows(self) -> None:
        consumed = PendingChatInjection.record(channel="C", slack_ts="1.0", text="old")
        assert consumed is not None
        consumed.consume()
        PendingChatInjection.record(channel="C", slack_ts="2.0", text="new")

        pending = list(PendingChatInjection.pending())
        assert [row.slack_ts for row in pending] == ["2.0"]

    def test_pending_returns_oldest_first(self) -> None:
        # Order by received_at; insert in reverse to prove ordering.
        later = PendingChatInjection.record(channel="C", slack_ts="2.0", text="later")
        earlier = PendingChatInjection.record(channel="C", slack_ts="1.0", text="earlier")
        assert later is not None
        assert earlier is not None
        # Force a deterministic received_at to remove insertion-time skew.
        earlier.received_at = timezone.now().replace(microsecond=0)
        later.received_at = earlier.received_at.replace(microsecond=1)
        earlier.save(update_fields=["received_at"])
        later.save(update_fields=["received_at"])

        pending = list(PendingChatInjection.pending())
        assert [row.slack_ts for row in pending] == ["1.0", "2.0"]

    def test_pending_filters_by_overlay_when_given(self) -> None:
        PendingChatInjection.record(channel="C", slack_ts="1.0", text="a", overlay="ovA")
        PendingChatInjection.record(channel="C", slack_ts="2.0", text="b", overlay="other")

        pending = list(PendingChatInjection.pending(overlay="ovA"))
        assert [row.overlay for row in pending] == ["ovA"]


class TestConsumeIdempotency:
    """The hook can re-fire safely — ``consume`` is single-use."""

    def test_first_consume_returns_true_and_stamps(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="1.0", text="x")
        assert row is not None

        assert row.consume() is True
        assert row.consumed_at is not None
        assert row.is_pending is False

    def test_second_consume_returns_false_no_op(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="1.0", text="x")
        assert row is not None
        row.consume()
        first_stamp = row.consumed_at

        assert row.consume() is False
        row.refresh_from_db()
        assert row.consumed_at == first_stamp


class TestStrRepr:
    def test_repr_for_pending_row(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="1.0", text="x", overlay="ovA")
        assert row is not None
        assert "pending" in str(row)
        assert "ovA" in str(row)
        assert "1.0" in str(row)

    def test_repr_for_consumed_row(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="1.0", text="x")
        assert row is not None
        row.consume()
        assert "consumed" in str(row)

    def test_repr_for_answered_row(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="1.0", text="x")
        assert row is not None
        PendingChatInjection.agent_answered_question("1.0")
        row.refresh_from_db()
        assert "answered" in str(row)


class TestAgentAnsweredQuestion:
    """``agent_answered_question`` stamps ``answered_at`` once."""

    def test_first_call_stamps(self) -> None:
        row = PendingChatInjection.record(channel="D", slack_ts="ts-1", text="why?")
        assert row is not None
        assert row.answered_at is None

        stamped = PendingChatInjection.agent_answered_question("ts-1")

        assert stamped == 1
        row.refresh_from_db()
        assert row.answered_at is not None

    def test_second_call_is_no_op(self) -> None:
        row = PendingChatInjection.record(channel="D", slack_ts="ts-1", text="why?")
        assert row is not None
        PendingChatInjection.agent_answered_question("ts-1")
        first_stamp = PendingChatInjection.objects.get(pk=row.pk).answered_at

        stamped = PendingChatInjection.agent_answered_question("ts-1")

        assert stamped == 0
        row.refresh_from_db()
        assert row.answered_at == first_stamp

    def test_empty_slack_ts_rejected(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="ts-1", text="why?")

        stamped = PendingChatInjection.agent_answered_question("")

        assert stamped == 0
        assert PendingChatInjection.objects.get().answered_at is None

    def test_unknown_ts_is_zero_stamped(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="ts-1", text="why?")

        stamped = PendingChatInjection.agent_answered_question("ts-not-here")

        assert stamped == 0

    def test_overlay_scoping(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="ts-x", text="why?", overlay="ovA")
        PendingChatInjection.record(channel="D", slack_ts="ts-x", text="why?", overlay="ovB")

        stamped = PendingChatInjection.agent_answered_question("ts-x", overlay="ovA")

        assert stamped == 1
        assert PendingChatInjection.objects.get(overlay="ovA").answered_at is not None
        assert PendingChatInjection.objects.get(overlay="ovB").answered_at is None


class TestUnansweredQuestionsSince:
    """Stop hook's main query — windowed + heuristic-filtered + unanswered."""

    def test_returns_question_rows_within_window(self) -> None:
        q1 = PendingChatInjection.record(channel="D", slack_ts="1", text="why is this red?")
        q2 = PendingChatInjection.record(channel="D", slack_ts="2", text="what about merging")
        PendingChatInjection.record(channel="D", slack_ts="3", text="t3 should merge its own PRs")

        rows = PendingChatInjection.unanswered_questions_since(timedelta(hours=1))

        slack_tss = [r.slack_ts for r in rows]
        assert "1" in slack_tss
        assert "2" in slack_tss
        assert "3" not in slack_tss
        assert q1 is not None
        assert q2 is not None

    def test_excludes_answered_rows(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="1", text="why?")
        PendingChatInjection.record(channel="D", slack_ts="2", text="what?")
        PendingChatInjection.agent_answered_question("1")

        rows = PendingChatInjection.unanswered_questions_since(timedelta(hours=1))

        assert [r.slack_ts for r in rows] == ["2"]

    def test_excludes_rows_outside_window(self) -> None:
        old = PendingChatInjection.record(channel="D", slack_ts="1", text="why is this red?")
        recent = PendingChatInjection.record(channel="D", slack_ts="2", text="what about now")
        assert old is not None
        assert recent is not None
        old.received_at = timezone.now() - timedelta(hours=3)
        old.save(update_fields=["received_at"])

        rows = PendingChatInjection.unanswered_questions_since(timedelta(hours=1))

        assert [r.slack_ts for r in rows] == ["2"]

    def test_empty_when_nothing_pending(self) -> None:
        assert PendingChatInjection.unanswered_questions_since(timedelta(hours=1)) == []

    def test_returns_oldest_first(self) -> None:
        first = PendingChatInjection.record(channel="D", slack_ts="2", text="why?")
        second = PendingChatInjection.record(channel="D", slack_ts="1", text="what?")
        assert first is not None
        assert second is not None
        first.received_at = timezone.now() - timedelta(minutes=30)
        second.received_at = timezone.now() - timedelta(minutes=10)
        first.save(update_fields=["received_at"])
        second.save(update_fields=["received_at"])

        rows = PendingChatInjection.unanswered_questions_since(timedelta(hours=1))

        assert [r.slack_ts for r in rows] == ["2", "1"]
