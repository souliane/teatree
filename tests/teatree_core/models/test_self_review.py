"""An author's self-review verdict is read back from the attempt its reviewing task recorded."""

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.models.review_verdict import Finding
from teatree.core.models.self_review import HeldFinding, SelfReview
from tests.teatree_core._self_review_helpers import (
    FAILURE_SCENARIO,
    HELD_SHA,
    LONG_SUMMARY,
    author_ticket,
    completed_self_review,
    head,
    self_review_result,
)


def _attempt(task: Task, result: dict[str, object], *, error: str = "") -> None:
    TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=0, error=error, result=result)


class TestLatestFor(TestCase):
    def test_the_newest_recorded_verdict_wins(self) -> None:
        ticket = author_ticket()
        completed_self_review(ticket, "hold")
        cleared = completed_self_review(ticket, "merge_safe")

        latest = SelfReview.latest_for(ticket)

        assert latest is not None
        assert (latest.task_pk, latest.is_hold) == (cleared.pk, False)
        assert SelfReview.open_hold_for(ticket) is None

    def test_a_failed_task_and_a_refused_attempt_are_not_a_self_review(self) -> None:
        ticket = author_ticket()
        held = completed_self_review(ticket, "hold")
        session = Session.objects.create(ticket=ticket, agent_id="review")
        failed = Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=Task.Status.FAILED)
        _attempt(failed, self_review_result("merge_safe"))
        refused = completed_self_review(ticket, "merge_safe")
        refused.attempts.update(error="anti-vacuity recording refused: missing anti_vacuity")

        latest = SelfReview.latest_for(ticket)

        assert latest is not None
        assert latest.task_pk == held.pk
        assert latest.reviewed_sha == HELD_SHA

    def test_a_refused_merge_safe_attempt_on_the_held_task_does_not_clear_it(self) -> None:
        ticket = author_ticket()
        held = completed_self_review(ticket, "hold")
        _attempt(held, self_review_result("merge_safe"), error="anti-vacuity recording refused: missing anti_vacuity")

        review = SelfReview.of_task(held)

        assert review is not None
        assert review.is_hold

    def test_a_later_verdictless_attempt_hides_the_hold_from_neither_reader(self) -> None:
        ticket = author_ticket()
        held = completed_self_review(ticket, "hold")
        _attempt(held, {"summary": "closed out of band"})

        assert SelfReview.of_task(held) == SelfReview.latest_for(ticket)
        assert SelfReview.open_hold_for(ticket) is not None

    def test_a_hold_parked_for_user_input_is_not_a_verdict(self) -> None:
        ticket = author_ticket()
        parked = completed_self_review(ticket, "hold")
        parked.attempts.update(result={**self_review_result("hold"), "needs_user_input": True})

        assert SelfReview.of_task(parked) is None
        assert SelfReview.latest_for(ticket) is None

    def test_the_verdict_is_normalised(self) -> None:
        ticket = author_ticket()
        completed_self_review(ticket, " HOLD ")

        assert SelfReview.open_hold_for(ticket) is not None

    def test_a_reviewer_ticket_has_no_self_review(self) -> None:
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.REVIEWER, state=Ticket.State.TESTED)
        completed_self_review(ticket, "hold")

        assert SelfReview.latest_for(ticket) is None

    def test_a_task_completed_out_of_band_has_no_verdict(self) -> None:
        ticket = author_ticket()
        session = Session.objects.create(ticket=ticket, agent_id="review")
        task = Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=Task.Status.COMPLETED)

        assert SelfReview.of_task(task) is None
        assert SelfReview.latest_for(ticket) is None


class TestTheDeliveryCycle(TestCase):
    def test_a_hold_from_a_shipped_cycle_does_not_govern_the_follow_up(self) -> None:
        ticket = author_ticket(state=Ticket.State.DELIVERED)
        completed_self_review(ticket, "hold")

        ticket.reopen_for_followup()
        ticket.save()

        assert SelfReview.latest_for(ticket) is None

    def test_a_hold_recorded_after_the_reopen_governs(self) -> None:
        ticket = author_ticket(state=Ticket.State.DELIVERED)
        completed_self_review(ticket, "merge_safe")
        ticket.reopen_for_followup()
        ticket.save()

        held = completed_self_review(ticket, "hold")

        review = SelfReview.open_hold_for(ticket)
        assert review is not None
        assert review.task_pk == held.pk


class TestHeldReviews(TestCase):
    def test_one_per_reviewing_task_however_many_attempts_re_reviews_of_a_head_each_count(self) -> None:
        ticket = author_ticket()
        many_attempts = completed_self_review(ticket, "hold", reviewed_sha=head(1), attempts=5)
        re_review = completed_self_review(ticket, "hold", reviewed_sha=head(1))
        moved = completed_self_review(ticket, "hold", reviewed_sha=head(2))
        completed_self_review(ticket, "merge_safe", reviewed_sha=head(3))

        assert SelfReview.held_reviews(ticket) == {many_attempts.pk, re_review.pk, moved.pk}

    def test_a_task_counts_by_its_newest_verdict(self) -> None:
        ticket = author_ticket()
        cleared = completed_self_review(ticket, "hold")
        _attempt(cleared, self_review_result("merge_safe"))

        assert SelfReview.held_reviews(ticket) == set()


class TestReworkReason(TestCase):
    def _hold(self, *findings: HeldFinding) -> SelfReview:
        return SelfReview(task_pk=7, verdict="hold", reviewed_sha=HELD_SHA, findings=findings)

    def test_each_finding_renders_whole_on_its_own_line_under_the_held_head(self) -> None:
        reason = self._hold(
            HeldFinding(Finding(severity="major", summary="untested branch", file="a.py", line=3)),
            HeldFinding(Finding(severity="minor", summary=LONG_SUMMARY, file="b.py", line=9)),
        ).rework_reason()

        assert f"Self-review HOLD at {HELD_SHA} (reviewing task 7)" in reason
        assert "\n- [major] a.py:3 — untested branch" in reason
        assert f"\n- [minor] b.py:9 — {LONG_SUMMARY}" in reason

    def test_a_failure_scenario_rides_under_its_finding(self) -> None:
        held = HeldFinding.from_dict(
            {
                "severity": "major",
                "summary": "replay re-mints",
                "file": "a.py",
                "line": 1,
                "failure_scenario": FAILURE_SCENARIO,
            }
        )

        reason = self._hold(held).rework_reason()

        assert f"- [major] a.py:1 — replay re-mints\n  failure scenario: {FAILURE_SCENARIO}" in reason

    def test_a_hold_with_no_findings_points_at_the_task(self) -> None:
        assert "no findings returned; read reviewing task 7" in self._hold().rework_reason()

    def test_the_reason_is_capped_with_an_explicit_elision(self) -> None:
        findings = [HeldFinding(Finding(severity="minor", summary=f"{index:03d} " + "y" * 590)) for index in range(100)]

        reason = self._hold(*findings).rework_reason()

        assert len(reason) <= 16_000
        assert "000 " in reason
        assert "099 " not in reason
        assert "more finding(s) elided; read reviewing task 7's result" in reason.splitlines()[-1]
