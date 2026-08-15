"""The whole-board plan-drift ratio the issue asked to surface (#4348).

A HOLD is what BLOCKS a re-fix, but it is not what the measurement counted: the
repo-wide 177-coding-against-29-planning shape includes every ticket that
re-implemented off one plan, held or not. So the pairs here are around the
THRESHOLD (two implementations off one plan is drift; one is not) and around the
overlap with the gate (a held ticket is reported as ``blocked``, never dropped) —
a detector that only ever named held tickets would restate the gate and would
have reported nothing for the very query that opened the issue.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import PullRequest, Session, Task, Ticket
from teatree.core.models.merge_clear import MergeClear
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.review_verdict import Finding, ReviewVerdict
from teatree.core.review.plan_drift import tickets_with_plan_drift

_SHA = "a1b2c3d4" * 5
_SLUG = "acme/widgets"
_PR_ID = 4306
_PR_URL = f"https://github.com/{_SLUG}/pull/{_PR_ID}"


def _author_ticket(*, state: str = Ticket.State.SHIPPED, issue: int = 755) -> Ticket:
    return Ticket.objects.create(
        role=Ticket.Role.AUTHOR,
        state=state,
        issue_url=f"https://github.com/{_SLUG}/issues/{issue}",
        overlay="t3-teatree",
    )


def _attach_pr(ticket: Ticket) -> PullRequest:
    return PullRequest.objects.create(ticket=ticket, url=_PR_URL, repo=_SLUG, iid=str(_PR_ID))


def _record_hold(ticket: Ticket, *, minutes_ago: int = 60) -> ReviewVerdict:
    verdict = ReviewVerdict.record(
        pr_id=_PR_ID,
        slug=_SLUG,
        reviewed_sha=_SHA,
        verdict=ReviewVerdict.Verdict.HOLD,
        reviewer_identity="cold-reviewer",
        findings=[Finding(severity="blocker", summary="fd/cwd half still fails open", file="a.py", line=8)],
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        ticket=ticket,
    )
    ReviewVerdict.objects.filter(pk=verdict.pk).update(recorded_at=timezone.now() - timedelta(minutes=minutes_ago))
    verdict.refresh_from_db()
    return verdict


def _record_plan(ticket: Ticket, *, minutes_ago: int) -> PlanArtifact:
    artifact = PlanArtifact.record(ticket=ticket, plan_text="defect class + every site", recorded_by="planning")
    PlanArtifact.objects.filter(pk=artifact.pk).update(recorded_at=timezone.now() - timedelta(minutes=minutes_ago))
    artifact.refresh_from_db()
    return artifact


def _coding_task(
    ticket: Ticket, *, status: str = Task.Status.PENDING, phase: str = "coding", minutes_ago: int = 0
) -> Task:
    """An implementing task, backdated when the case turns on plan-vs-task ordering."""
    session = Session.objects.create(ticket=ticket, agent_id=phase)
    task = Task.objects.create(
        ticket=ticket, session=session, phase=phase, status=status, execution_reason="re-fix the findings"
    )
    if minutes_ago:
        Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - timedelta(minutes=minutes_ago))
        task.refresh_from_db()
    return task


class TestPlanDriftReport(TestCase):
    """The threshold, the re-plan reset, the gate overlap, and the closed-state exclusion."""

    def test_a_ticket_re_implemented_twice_off_one_plan_is_reported(self) -> None:
        ticket = _author_ticket(state=Ticket.State.CODED)
        _record_plan(ticket, minutes_ago=90)
        _coding_task(ticket, status=Task.Status.COMPLETED)
        _coding_task(ticket)

        rows = tickets_with_plan_drift()

        assert [row.ticket_id for row in rows] == [ticket.pk]
        assert rows[0].coding_tasks_since_last_plan == 2
        assert rows[0].blocked is False

    def test_one_implementation_per_plan_is_not_drift(self) -> None:
        ticket = _author_ticket(state=Ticket.State.CODED)
        _record_plan(ticket, minutes_ago=90)
        _coding_task(ticket)

        assert tickets_with_plan_drift() == []

    def test_a_re_plan_between_the_two_implementations_clears_the_drift(self) -> None:
        """One implementation per plan, interleaved — the healthy shape the detector must not flag."""
        ticket = _author_ticket(state=Ticket.State.CODED)
        _record_plan(ticket, minutes_ago=90)
        _coding_task(ticket, status=Task.Status.COMPLETED, minutes_ago=60)
        _record_plan(ticket, minutes_ago=30)
        _coding_task(ticket)

        assert tickets_with_plan_drift() == []

    def test_a_held_ticket_is_reported_as_blocked_rather_than_dropped(self) -> None:
        ticket = _author_ticket()
        _attach_pr(ticket)
        _record_plan(ticket, minutes_ago=90)
        _record_hold(ticket)
        _coding_task(ticket, status=Task.Status.COMPLETED)
        _coding_task(ticket)

        rows = tickets_with_plan_drift()

        assert [(row.ticket_id, row.blocked) for row in rows] == [(ticket.pk, True)]

    def test_a_delivered_ticket_is_not_reported(self) -> None:
        ticket = _author_ticket(state=Ticket.State.DELIVERED)
        _record_plan(ticket, minutes_ago=90)
        _coding_task(ticket, status=Task.Status.COMPLETED)
        _coding_task(ticket, status=Task.Status.COMPLETED)

        assert tickets_with_plan_drift() == []

    def test_the_report_is_scoped_by_overlay(self) -> None:
        ticket = _author_ticket(state=Ticket.State.CODED)
        _record_plan(ticket, minutes_ago=90)
        _coding_task(ticket, status=Task.Status.COMPLETED)
        _coding_task(ticket)

        assert [row.ticket_id for row in tickets_with_plan_drift(overlay="t3-teatree")] == [ticket.pk]
        assert tickets_with_plan_drift(overlay="other") == []
