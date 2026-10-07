"""A self-review HOLD loop is bounded per held head: past the cap one INTERNAL question, never an owner page."""

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import DeferredQuestion, Task, Ticket
from teatree.core.repair_loop import max_phase_iterations
from tests.teatree_core._self_review_helpers import HELD_FINDINGS, author_ticket, completed_self_review, head

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

    def test_re_reviews_of_one_head_attempt_rows_and_merge_safe_reviews_do_not_count(self) -> None:
        ticket = author_ticket()
        completed_self_review(ticket, "hold", reviewed_sha=head(1), attempts=max_phase_iterations() + 1)
        for index in range(max_phase_iterations()):
            completed_self_review(ticket, "hold", reviewed_sha=head(1))
            completed_self_review(ticket, "merge_safe", reviewed_sha=head(100 + index))
        latest = completed_self_review(ticket, "hold", reviewed_sha=_NEW_HEAD)

        _apply(latest)

        assert Task.objects.filter(ticket=ticket, phase="coding", parent_task=latest).exists()
        assert not DeferredQuestion.objects.exists()
