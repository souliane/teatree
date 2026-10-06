"""An author's self-review verdict is read back from the attempt its reviewing task recorded."""

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.models.review_verdict import Finding
from teatree.core.models.self_review import SelfReview
from tests.teatree_core._self_review_helpers import HELD_SHA, author_ticket, completed_self_review, self_review_result


class TestLatestFor(TestCase):
    def test_the_newest_recorded_verdict_wins(self) -> None:
        ticket = author_ticket()
        completed_self_review(ticket, "hold")
        cleared = completed_self_review(ticket, "merge_safe")

        latest = SelfReview.latest_for(ticket)

        assert latest is not None
        assert (latest.task_pk, latest.is_hold) == (cleared.pk, False)

    def test_a_failed_task_and_a_refused_attempt_are_not_a_self_review(self) -> None:
        ticket = author_ticket()
        held = completed_self_review(ticket, "hold")
        session = Session.objects.create(ticket=ticket, agent_id="review")
        failed = Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=Task.Status.FAILED)
        TaskAttempt.objects.create(task=failed, ended_at=timezone.now(), result=self_review_result("merge_safe"))
        refused = completed_self_review(ticket, "merge_safe")
        refused.attempts.update(error="anti-vacuity recording refused: missing anti_vacuity")

        latest = SelfReview.latest_for(ticket)

        assert latest is not None
        assert latest.task_pk == held.pk
        assert latest.reviewed_sha == HELD_SHA

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


class TestReworkReason(TestCase):
    def _hold(self, *findings: Finding) -> SelfReview:
        return SelfReview(task_pk=7, verdict="hold", reviewed_sha=HELD_SHA, findings=findings)

    def test_each_finding_renders_on_its_own_line_under_the_held_head(self) -> None:
        reason = self._hold(Finding(severity="major", summary="untested branch", file="a.py", line=3)).rework_reason()

        assert f"Self-review HOLD at {HELD_SHA} (reviewing task 7)" in reason
        assert "\n- [major] a.py:3 — untested branch" in reason

    def test_a_hold_with_no_findings_points_at_the_task(self) -> None:
        assert "no findings returned; read reviewing task 7" in self._hold().rework_reason()

    def test_a_long_summary_is_clipped(self) -> None:
        reason = self._hold(Finding(severity="minor", summary="x" * 5000)).rework_reason()

        assert "x" * 600 not in reason
        assert "x" * 590 in reason

    def test_the_reason_is_capped_with_an_explicit_elision(self) -> None:
        findings = [Finding(severity="minor", summary=f"{index:03d} " + "y" * 590) for index in range(100)]

        reason = self._hold(*findings).rework_reason()

        assert len(reason) <= 16_000
        assert "000 " in reason
        assert "099 " not in reason
        assert reason.splitlines()[-1].startswith("- ")
        assert "more finding(s) elided" in reason.splitlines()[-1]
