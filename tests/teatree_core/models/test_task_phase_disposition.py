"""A self-review HOLD rework loop is bounded per lap: past the cap one INTERNAL question, never an owner page."""

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.agents.attempt_recorder import record_result_envelope
from teatree.core.models import DeferredQuestion, Session, Task, Ticket
from teatree.core.repair_loop import max_phase_iterations
from tests.teatree_core._self_review_helpers import (
    HELD_FINDINGS,
    HELD_SHA,
    author_ticket,
    completed_self_review,
    head,
    self_review_result,
)

_NEW_HEAD = head(999)


def _apply(task: Task) -> bool:
    with patch.object(Ticket, "has_shippable_diff", return_value=True):
        return task._apply_phase_transition()


def _held_at_distinct_heads(ticket: Ticket, count: int) -> None:
    for index in range(count):
        completed_self_review(ticket, "hold", reviewed_sha=head(index))


class TestTheHoldReworkLoopIsBounded(TestCase):
    def test_at_the_cap_the_next_hold_records_one_internal_question_carrying_the_findings(self) -> None:
        ticket = author_ticket()
        _held_at_distinct_heads(ticket, max_phase_iterations())
        latest = completed_self_review(ticket, "hold", reviewed_sha=_NEW_HEAD)

        assert _apply(latest) is False
        assert _apply(latest) is False

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.TESTED
        assert not Task.objects.filter(ticket=ticket, phase="coding").exists()
        [question] = DeferredQuestion.objects.all()
        assert question.dedupe_marker == f"self-review-hold-cap:{ticket.pk}"
        assert question.audience == DeferredQuestion.Audience.INTERNAL
        assert f"reviewing task {latest.pk}" in question.question
        assert str(HELD_FINDINGS[0]["summary"]) in question.question

    def test_the_cap_question_is_never_re_raised_once_dismissed_or_answered(self) -> None:
        ticket = author_ticket()
        _held_at_distinct_heads(ticket, max_phase_iterations())
        latest = completed_self_review(ticket, "hold", reviewed_sha=_NEW_HEAD)
        _apply(latest)

        DeferredQuestion.objects.update(dismissed_at=timezone.now(), dismissed_reason="seen")
        _apply(latest)
        DeferredQuestion.objects.update(answered_at=timezone.now(), answer_text="ignore it")
        _apply(latest)

        assert DeferredQuestion.objects.count() == 1

    def test_below_the_cap_the_hold_still_queues_its_rework(self) -> None:
        ticket = author_ticket()
        _held_at_distinct_heads(ticket, max_phase_iterations() - 1)
        latest = completed_self_review(ticket, "hold", reviewed_sha=_NEW_HEAD)

        _apply(latest)

        assert Task.objects.filter(ticket=ticket, phase="coding", parent_task=latest).exists()
        assert not DeferredQuestion.objects.exists()

    def test_each_hold_review_of_one_unchanged_head_spends_a_lap(self) -> None:
        ticket = author_ticket()
        for _ in range(max_phase_iterations()):
            completed_self_review(ticket, "hold")
        latest = completed_self_review(ticket, "hold")

        _apply(latest)

        assert not Task.objects.filter(ticket=ticket, phase="coding").exists()
        assert DeferredQuestion.objects.filter(dedupe_marker=f"self-review-hold-cap:{ticket.pk}").count() == 1

    def test_attempt_rows_and_merge_safe_reviews_do_not_spend_a_lap(self) -> None:
        ticket = author_ticket()
        completed_self_review(ticket, "hold", reviewed_sha=head(1), attempts=max_phase_iterations() + 1)
        for index in range(max_phase_iterations()):
            completed_self_review(ticket, "merge_safe", reviewed_sha=head(100 + index))
        latest = completed_self_review(ticket, "hold", reviewed_sha=_NEW_HEAD)

        _apply(latest)

        assert Task.objects.filter(ticket=ticket, phase="coding", parent_task=latest).exists()
        assert not DeferredQuestion.objects.exists()


def _lap(ticket: Ticket, *, reviewed_sha: str) -> bool:
    """One HOLD self-review through the real recorder, then its rework and testing; ``False`` once none is queued."""
    review = Task.objects.filter(ticket=ticket, phase="reviewing", status=Task.Status.PENDING).order_by("-pk").first()
    if review is None:
        review = Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket, agent_id="review"), phase="reviewing"
        )
    review.claim(claimed_by="self-reviewer")
    with patch.object(Ticket, "has_shippable_diff", return_value=True):
        record_result_envelope(review, self_review_result("hold", reviewed_sha=reviewed_sha), phase="reviewing")
        rework = Task.objects.filter(ticket=ticket, phase="coding", parent_task=review).first()
        if rework is None:
            return False
        rework.claim(claimed_by="coder")
        rework.complete()
        testing = Task.objects.get(ticket=ticket, phase="testing", status=Task.Status.PENDING)
        testing.claim(claimed_by="tester")
        testing.complete()
    return True


class TestTheLoopStopsThroughTheRealTransitions(TestCase):
    def _laps_until_stopped(self, ticket: Ticket, *, distinct_heads: bool) -> int:
        laps = 0
        while laps <= 2 * max_phase_iterations():
            if not _lap(ticket, reviewed_sha=head(laps) if distinct_heads else HELD_SHA):
                return laps
            laps += 1
        return laps

    def test_a_rework_that_changes_nothing_stops_at_the_cap(self) -> None:
        ticket = author_ticket()

        laps = self._laps_until_stopped(ticket, distinct_heads=False)

        assert laps == max_phase_iterations()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.TESTED
        [question] = DeferredQuestion.objects.all()
        assert question.audience == DeferredQuestion.Audience.INTERNAL

    def test_reworks_that_each_move_the_head_stop_at_the_cap_too(self) -> None:
        ticket = author_ticket()

        laps = self._laps_until_stopped(ticket, distinct_heads=True)

        assert laps == max_phase_iterations()
        assert DeferredQuestion.objects.count() == 1
