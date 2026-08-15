"""A re-fix after a review HOLD is re-planned first — findings are not a work order (#4348).

Each rule is pinned by a symmetric must-block / must-NOT-block pair, because the
whole defect is that "does this ticket have a plan?" answered YES for every held
ticket: the plan existed, from the ORIGINAL implementation, days and several
review passes stale. The predicate under test is a COMPARISON, never a presence
check, so the passing direction (a plan recorded AFTER the verdict) is as
load-bearing as the blocking one.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import PullRequest, Session, Task, Ticket
from teatree.core.models.merge_clear import MergeClear
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.review_verdict import Finding, ReviewVerdict
from teatree.core.models.trivial_plan_skip import mark_trivial_plan_skip
from teatree.core.review.refix_plan import (
    blocked_refix_task_pks,
    coding_tasks_since_last_plan,
    not_awaiting_refix_plan_q,
    refix_plan_stale_reason,
    tickets_awaiting_refix_plan,
)

_SHA = "a1b2c3d4" * 5
_SLUG = "acme/widgets"
_PR_ID = 4306
_PR_URL = f"https://github.com/{_SLUG}/pull/{_PR_ID}"
_REVIEWER = "cold-reviewer"


def _author_ticket(*, state: str = Ticket.State.SHIPPED) -> Ticket:
    return Ticket.objects.create(
        role=Ticket.Role.AUTHOR,
        state=state,
        issue_url="https://github.com/acme/widgets/issues/755",
        overlay="t3-teatree",
    )


def _attach_pr(ticket: Ticket) -> PullRequest:
    return PullRequest.objects.create(ticket=ticket, url=_PR_URL, repo=_SLUG, iid=str(_PR_ID))


def _record_hold(ticket: Ticket | None = None, *, minutes_ago: int = 60, sha: str = _SHA) -> ReviewVerdict:
    verdict = ReviewVerdict.record(
        pr_id=_PR_ID,
        slug=_SLUG,
        reviewed_sha=sha,
        verdict=ReviewVerdict.Verdict.HOLD,
        reviewer_identity=_REVIEWER,
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


def _coding_task(ticket: Ticket, *, status: str = Task.Status.PENDING, phase: str = "coding") -> Task:
    session = Session.objects.create(ticket=ticket, agent_id=phase)
    return Task.objects.create(
        ticket=ticket, session=session, phase=phase, status=status, execution_reason="re-fix the findings"
    )


class TestRefixPlanStaleReason(TestCase):
    """The comparison: a plan OLDER than the newest HOLD is treated as absent."""

    def test_a_plan_predating_the_hold_blocks(self) -> None:
        ticket = _author_ticket()
        _attach_pr(ticket)
        _record_plan(ticket, minutes_ago=4000)
        _record_hold()

        reason = refix_plan_stale_reason(ticket)

        assert reason
        assert "plan-reaffirm" in reason
        assert str(ticket.pk) in reason

    def test_a_plan_recorded_after_the_hold_admits(self) -> None:
        ticket = _author_ticket()
        _attach_pr(ticket)
        _record_hold(minutes_ago=60)
        _record_plan(ticket, minutes_ago=1)

        assert refix_plan_stale_reason(ticket) == ""

    def test_a_ticket_with_no_hold_verdict_admits(self) -> None:
        """The must-NOT-fire direction — ordinary work is never over-blocked."""
        ticket = _author_ticket(state=Ticket.State.PLANNED)
        _record_plan(ticket, minutes_ago=4000)

        assert refix_plan_stale_reason(ticket) == ""

    def test_a_merge_safe_verdict_is_not_a_hold(self) -> None:
        ticket = _author_ticket()
        _attach_pr(ticket)
        _record_plan(ticket, minutes_ago=4000)
        ReviewVerdict.record(
            pr_id=_PR_ID,
            slug=_SLUG,
            reviewed_sha=_SHA,
            verdict=ReviewVerdict.Verdict.MERGE_SAFE,
            reviewer_identity=_REVIEWER,
        )

        assert refix_plan_stale_reason(ticket) == ""

    def test_a_held_ticket_with_no_plan_at_all_blocks(self) -> None:
        ticket = _author_ticket()
        _attach_pr(ticket)
        _record_hold()

        assert "no plan recorded" in refix_plan_stale_reason(ticket)

    def test_a_hold_bound_only_by_the_reviewer_ticket_fk_still_blocks_the_author(self) -> None:
        """#4366's two-row shape: the verdict hangs off the REVIEWER ticket, the coding task off the author."""
        author = _author_ticket()
        _attach_pr(author)
        _record_plan(author, minutes_ago=4000)
        reviewer = Ticket.objects.create(role=Ticket.Role.REVIEWER, state=Ticket.State.REVIEW_POSTED, issue_url=_PR_URL)
        _record_hold(reviewer)

        assert refix_plan_stale_reason(author)

    def test_a_pr_recorded_only_in_ticket_extra_still_binds(self) -> None:
        """A PR opened before the row write existed is recorded only in ``extra``."""
        ticket = _author_ticket()
        ticket.merge_extra(set_keys={"pr_urls": [_PR_URL]})
        ticket.refresh_from_db()
        _record_plan(ticket, minutes_ago=4000)
        _record_hold()

        assert refix_plan_stale_reason(ticket)

    def test_a_reviewer_role_ticket_is_never_blocked(self) -> None:
        """A reviewer row never implements, and never carries a plan — it must not read as stale."""
        reviewer = Ticket.objects.create(role=Ticket.Role.REVIEWER, state=Ticket.State.REVIEW_POSTED, issue_url=_PR_URL)
        _record_hold(reviewer)

        assert refix_plan_stale_reason(reviewer) == ""

    def test_a_trivial_skip_recorded_after_the_hold_admits(self) -> None:
        ticket = _author_ticket()
        _attach_pr(ticket)
        _record_hold(minutes_ago=60)
        mark_trivial_plan_skip(ticket, reason="one-line constant bump", by="souliane")
        ticket.refresh_from_db()

        assert refix_plan_stale_reason(ticket) == ""

    def test_a_trivial_skip_recorded_before_the_hold_still_blocks(self) -> None:
        """The carve-out is a decision about THIS re-fix, so a pre-verdict skip cannot excuse it."""
        ticket = _author_ticket()
        _attach_pr(ticket)
        mark_trivial_plan_skip(ticket, reason="one-line constant bump", by="souliane")
        ticket.refresh_from_db()
        _record_hold(minutes_ago=0)

        assert refix_plan_stale_reason(ticket)


class TestCodingTasksSinceLastPlan(TestCase):
    """The cheap detector the issue names — nothing reported it before."""

    def test_counts_only_coding_tasks_created_after_the_newest_plan(self) -> None:
        ticket = _author_ticket()
        first = _coding_task(ticket, status=Task.Status.COMPLETED)
        Task.objects.filter(pk=first.pk).update(created_at=timezone.now() - timedelta(days=3))
        _record_plan(ticket, minutes_ago=2880)
        _coding_task(ticket, status=Task.Status.COMPLETED)
        _coding_task(ticket)

        assert coding_tasks_since_last_plan(ticket) == 2

    def test_a_ticket_with_no_plan_counts_every_coding_task(self) -> None:
        ticket = _author_ticket()
        _coding_task(ticket, status=Task.Status.COMPLETED)

        assert coding_tasks_since_last_plan(ticket) == 1

    def test_a_debugging_task_counts_as_implementing(self) -> None:
        ticket = _author_ticket()
        _record_plan(ticket, minutes_ago=60)
        _coding_task(ticket, phase="debugging")

        assert coding_tasks_since_last_plan(ticket) == 1

    def test_a_reviewing_task_does_not_count(self) -> None:
        ticket = _author_ticket()
        _record_plan(ticket, minutes_ago=60)
        _coding_task(ticket, phase="reviewing")

        assert coding_tasks_since_last_plan(ticket) == 0


class TestClaimBoundary(TestCase):
    """No implementing task is CLAIMABLE while the plan predates the newest HOLD."""

    def test_the_blocked_coding_task_is_not_claimable(self) -> None:
        ticket = _author_ticket()
        _attach_pr(ticket)
        _record_plan(ticket, minutes_ago=4000)
        _record_hold()
        task = _coding_task(ticket)

        assert task.pk in blocked_refix_task_pks()
        claimable = set(Task.objects.filter(not_awaiting_refix_plan_q()).values_list("pk", flat=True))
        assert task.pk not in claimable

    def test_the_same_task_is_claimable_once_the_plan_is_re_recorded(self) -> None:
        """The RED control's mirror: re-planning releases the block with no other change."""
        ticket = _author_ticket()
        _attach_pr(ticket)
        _record_plan(ticket, minutes_ago=4000)
        _record_hold(minutes_ago=60)
        task = _coding_task(ticket)
        _record_plan(ticket, minutes_ago=0)

        assert blocked_refix_task_pks() == []
        assert task.pk in set(Task.objects.filter(not_awaiting_refix_plan_q()).values_list("pk", flat=True))

    def test_a_reviewing_task_on_the_same_held_ticket_stays_claimable(self) -> None:
        """The block is scoped to IMPLEMENTING phases — a held PR must still be re-reviewable."""
        ticket = _author_ticket()
        _attach_pr(ticket)
        _record_plan(ticket, minutes_ago=4000)
        _record_hold()
        reviewing = _coding_task(ticket, phase="reviewing")

        assert reviewing.pk in set(Task.objects.filter(not_awaiting_refix_plan_q()).values_list("pk", flat=True))

    def test_an_unheld_ticket_coding_task_stays_claimable(self) -> None:
        ticket = _author_ticket(state=Ticket.State.PLANNED)
        _record_plan(ticket, minutes_ago=10)
        task = _coding_task(ticket)

        assert task.pk in set(Task.objects.filter(not_awaiting_refix_plan_q()).values_list("pk", flat=True))


class TestAwaitingRefixPlanReport(TestCase):
    """The surface — the ratio was hand-queried out of the control DB before."""

    def test_reports_the_held_ticket_with_its_detector_value(self) -> None:
        ticket = _author_ticket()
        _attach_pr(ticket)
        _record_plan(ticket, minutes_ago=4000)
        _record_hold(minutes_ago=120)
        _coding_task(ticket, status=Task.Status.COMPLETED)
        _coding_task(ticket)

        rows = tickets_awaiting_refix_plan()

        assert [row.ticket_id for row in rows] == [ticket.pk]
        assert rows[0].coding_tasks_since_last_plan == 2
        assert rows[0].open_implementing_tasks == 1
        assert rows[0].plan_recorded_at is not None

    def test_a_replanned_ticket_leaves_the_report(self) -> None:
        ticket = _author_ticket()
        _attach_pr(ticket)
        _record_hold(minutes_ago=120)
        _record_plan(ticket, minutes_ago=0)

        assert tickets_awaiting_refix_plan() == []

    def test_the_report_is_scoped_by_overlay(self) -> None:
        ticket = _author_ticket()
        _attach_pr(ticket)
        _record_hold()

        assert [row.ticket_id for row in tickets_awaiting_refix_plan(overlay="t3-teatree")] == [ticket.pk]
        assert tickets_awaiting_refix_plan(overlay="other-overlay") == []
