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


class TestLoopUnrepliedQuery:
    """The reactive Slack-answer loop reads its work via ``loop_unreplied`` (#1014/#1075).

    ``loop_unreplied`` is orthogonal to ``pending`` AND to #1069's
    ``answered_at`` turn-end gate: it gates on ``loop_replied_at`` (a
    column distinct from both ``consumed_at`` and ``answered_at``), so a
    row drained into the prompt (``consumed``) is still *loop-unreplied*
    until the answer loop posts a reply — and a loop reply never touches
    ``answered_at`` (#1075 / Option B).
    """

    def test_loop_unreplied_excludes_loop_replied_rows(self) -> None:
        replied = PendingChatInjection.record(channel="C", slack_ts="1.0", text="old")
        assert replied is not None
        replied.mark_loop_replied("ack")
        PendingChatInjection.record(channel="C", slack_ts="2.0", text="new")

        loop_unreplied = list(PendingChatInjection.loop_unreplied())
        assert [row.slack_ts for row in loop_unreplied] == ["2.0"]

    def test_loop_unreplied_returns_oldest_first(self) -> None:
        later = PendingChatInjection.record(channel="C", slack_ts="2.0", text="later")
        earlier = PendingChatInjection.record(channel="C", slack_ts="1.0", text="earlier")
        assert later is not None
        assert earlier is not None
        earlier.received_at = timezone.now().replace(microsecond=0)
        later.received_at = earlier.received_at.replace(microsecond=1)
        earlier.save(update_fields=["received_at"])
        later.save(update_fields=["received_at"])

        loop_unreplied = list(PendingChatInjection.loop_unreplied())
        assert [row.slack_ts for row in loop_unreplied] == ["1.0", "2.0"]

    def test_loop_unreplied_filters_by_overlay_when_given(self) -> None:
        PendingChatInjection.record(channel="C", slack_ts="1.0", text="a", overlay="ovA")
        PendingChatInjection.record(channel="C", slack_ts="2.0", text="b", overlay="other")

        loop_unreplied = list(PendingChatInjection.loop_unreplied(overlay="ovA"))
        assert [row.overlay for row in loop_unreplied] == ["ovA"]

    def test_consumed_row_is_still_loop_unreplied(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="1.0", text="x")
        assert row is not None
        row.consume()

        assert list(PendingChatInjection.loop_unreplied()) == [row]
        assert row.loop_replied_at is None
        assert row.consumed_at is not None


class TestMarkLoopReplied:
    """``mark_loop_replied`` is a single-use compare-and-swap, like ``consume``."""

    def test_first_mark_returns_true_and_stamps_kind(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="1.0", text="x")
        assert row is not None

        assert row.mark_loop_replied("simple") is True
        assert row.loop_replied_at is not None
        assert row.answer_kind == "simple"
        assert row.is_loop_replied is True

    def test_second_mark_returns_false_no_op(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="1.0", text="x")
        assert row is not None
        row.mark_loop_replied("ack")
        first_stamp = row.loop_replied_at

        assert row.mark_loop_replied("delegated") is False
        row.refresh_from_db()
        assert row.loop_replied_at == first_stamp
        assert row.answer_kind == "ack"

    def test_loop_replied_is_orthogonal_to_consumed(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="1.0", text="x")
        assert row is not None

        assert row.consume() is True
        assert row.mark_loop_replied("simple") is True

        row.refresh_from_db()
        assert row.consumed_at is not None
        assert row.loop_replied_at is not None
        assert row.answer_kind == "simple"

    def test_loop_replied_does_not_touch_answered_at(self) -> None:
        """Option B (#1075) keystone: a loop reply must NOT satisfy the #1069 gate.

        ``mark_loop_replied`` stamps only ``loop_replied_at``; it must
        leave ``answered_at`` NULL so the #1063 turn-end Stop-hook gate
        still fires for a question the loop "handled" but the agent never
        personally answered. Regression guard for the shared-column
        blocker resolved by Option B.
        """
        row = PendingChatInjection.record(channel="C", slack_ts="1.0", text="why does X fail?")
        assert row is not None

        assert row.mark_loop_replied("simple") is True
        row.refresh_from_db()

        assert row.loop_replied_at is not None
        assert row.answered_at is None  # the gate column is untouched

        # The #1069 turn-end gate still sees this question as unanswered:
        # a token-cheap loop reply did NOT satisfy "agent personally replied".
        still_unanswered = PendingChatInjection.unanswered_questions_since(timedelta(hours=1))
        assert [r.slack_ts for r in still_unanswered] == ["1.0"]


class TestMarkEyesReacted:
    """The :eyes: ack-reaction must fire at most once across re-runs."""

    def test_first_mark_returns_true_and_stamps(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="1.0", text="x")
        assert row is not None

        assert row.mark_eyes_reacted() is True
        assert row.eyes_reacted_at is not None

    def test_second_mark_returns_false_no_op(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="1.0", text="x")
        assert row is not None
        row.mark_eyes_reacted()
        first_stamp = row.eyes_reacted_at

        assert row.mark_eyes_reacted() is False
        row.refresh_from_db()
        assert row.eyes_reacted_at == first_stamp


class TestUnmarkEyesReacted:
    """Releasing the :eyes: claim so a failed reaction is retried next cycle."""

    def test_unmark_clears_the_stamp_and_allows_remark(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="1.0", text="x")
        assert row is not None
        row.mark_eyes_reacted()

        assert row.unmark_eyes_reacted() is True
        assert row.eyes_reacted_at is None
        assert row.mark_eyes_reacted() is True

    def test_unmark_on_unstamped_row_is_a_no_op(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="1.0", text="x")
        assert row is not None

        assert row.unmark_eyes_reacted() is False
        assert row.eyes_reacted_at is None


class TestUnmarkLoopReplied:
    """Releasing the loop-reply claim so a failed ack reaction is retried."""

    def test_unmark_clears_stamp_and_kind_and_allows_remark(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="1.0", text="x")
        assert row is not None
        row.mark_loop_replied("ack")

        assert row.unmark_loop_replied() is True
        assert row.loop_replied_at is None
        assert row.answer_kind == ""
        assert row.mark_loop_replied("delegated") is True
        assert row.answer_kind == "delegated"

    def test_unmark_on_unreplied_row_is_a_no_op(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="1.0", text="x")
        assert row is not None

        assert row.unmark_loop_replied() is False
        assert row.loop_replied_at is None


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

    def test_stamps_every_row_sharing_the_ts(self) -> None:
        """The stamp keys on ``slack_ts`` alone — it does not narrow by overlay.

        Two rows sharing a ``slack_ts`` are the same user DM recorded under
        two overlays (the scanner ran in both). They are the same question,
        so answering it stamps both — keeping the stamp symmetric with the
        unscoped gate. The old exact-overlay filter stamped at most one,
        stranding the other row unanswered so the gate nagged forever.
        """
        PendingChatInjection.record(channel="D", slack_ts="ts-x", text="why?", overlay="ovA")
        PendingChatInjection.record(channel="D", slack_ts="ts-x", text="why?", overlay="ovB")

        stamped = PendingChatInjection.agent_answered_question("ts-x")

        assert stamped == 2
        assert PendingChatInjection.objects.get(overlay="ovA").answered_at is not None
        assert PendingChatInjection.objects.get(overlay="ovB").answered_at is not None

    def test_cross_overlay_answer_clears_gate(self) -> None:
        """Sub-case (b): the user's real concurrent multi-overlay deployment.

        One overlay's session records the question; a *different* overlay's
        session answers it. The old exact-overlay filter stamped 0 rows
        here (the answering overlay never matched the recording overlay),
        so ``answered_at`` stayed NULL and the unscoped gate nagged forever.
        Keying the stamp on ``slack_ts`` alone clears the gate regardless.
        """
        PendingChatInjection.record(
            channel="D", slack_ts="ts-cross", text="why does this fail?", overlay="overlay-alpha"
        )

        unanswered_before = PendingChatInjection.unanswered_questions_since(timedelta(hours=1))
        assert [r.slack_ts for r in unanswered_before] == ["ts-cross"]

        stamped = PendingChatInjection.agent_answered_question("ts-cross")

        assert stamped == 1
        assert PendingChatInjection.unanswered_questions_since(timedelta(hours=1)) == []

    def test_genuinely_unanswered_row_still_nags(self) -> None:
        """The gate must not be weakened: answering one ts leaves others nagging."""
        PendingChatInjection.record(channel="D", slack_ts="ts-answered", text="why?", overlay="overlay-alpha")
        PendingChatInjection.record(channel="D", slack_ts="ts-open", text="what about this?", overlay="overlay-beta")

        PendingChatInjection.agent_answered_question("ts-answered")

        still_nagging = PendingChatInjection.unanswered_questions_since(timedelta(hours=1))
        assert [r.slack_ts for r in still_nagging] == ["ts-open"]

    def test_gate_clears_after_answer(self) -> None:
        """The Stop-hook gate must clear once the satisfier stamps the row.

        Record under a concrete overlay, answer it, and the gate must
        return an empty list — the permanent-nag is eliminated.
        """
        PendingChatInjection.record(channel="D", slack_ts="ts-q2", text="why does this fail?", overlay="overlay-alpha")

        unanswered_before = PendingChatInjection.unanswered_questions_since(timedelta(hours=1))
        assert [r.slack_ts for r in unanswered_before] == ["ts-q2"]

        PendingChatInjection.agent_answered_question("ts-q2")

        unanswered_after = PendingChatInjection.unanswered_questions_since(timedelta(hours=1))
        assert unanswered_after == []


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


class TestRetireAnsweredInThread:
    """``retire_answered_in_thread`` stamps the question a threaded reply answers (#2053)."""

    def test_matching_thread_ts_stamps_both_gates(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="1700.0001", text="why?")

        stamped = PendingChatInjection.retire_answered_in_thread("1700.0001")

        row = PendingChatInjection.objects.get()
        assert stamped == 1
        assert row.loop_replied_at is not None
        assert row.answered_at is not None
        assert row.answer_kind == PendingChatInjection.AnswerKind.QUESTION_REPLY

    def test_empty_thread_ts_is_a_no_op(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="1700.0001", text="why?")

        assert PendingChatInjection.retire_answered_in_thread("") == 0
        assert PendingChatInjection.objects.get().loop_replied_at is None

    def test_unknown_thread_ts_matches_nothing(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="1700.0001", text="why?")

        assert PendingChatInjection.retire_answered_in_thread("9999.9999") == 0
        assert PendingChatInjection.objects.get().loop_replied_at is None

    def test_does_not_overwrite_an_existing_loop_reply(self) -> None:
        row = PendingChatInjection.record(channel="D", slack_ts="1700.0001", text="why?")
        assert row is not None
        row.mark_loop_replied(PendingChatInjection.AnswerKind.ACK)

        assert PendingChatInjection.retire_answered_in_thread("1700.0001") == 0
        row.refresh_from_db()
        assert row.answer_kind == PendingChatInjection.AnswerKind.ACK

    def test_second_call_is_idempotent(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="1700.0001", text="why?")

        assert PendingChatInjection.retire_answered_in_thread("1700.0001") == 1
        first_stamp = PendingChatInjection.objects.get().loop_replied_at
        assert PendingChatInjection.retire_answered_in_thread("1700.0001") == 0
        assert PendingChatInjection.objects.get().loop_replied_at == first_stamp
