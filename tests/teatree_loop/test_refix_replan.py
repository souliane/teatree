"""Routing a held re-fix through planning, end to end (#4348).

The block on its own would trade an unplanned implementer for a plan nobody ever
writes — silent, and worse, because the board still reads as progressing. So the
pairs here are: the implementing task is refused AND a planning task appears; the
planning task's brief demands the defect CLASS and the site enumeration; and a
ticket that is not held is untouched by either half.
"""

from datetime import timedelta

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import PullRequest, Session, Task, Ticket
from teatree.core.models.errors import NoCurrentPlanError
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.review_verdict import Finding, ReviewVerdict
from teatree.core.review.refix_plan import claimable_dispatch_q
from teatree.loop.refix_replan import route_refix_to_planning
from teatree.loop.tick_recovery import _reap_stale_task_claims

_SHA = "b1b2c3d4" * 5
_SLUG = "acme/widgets"
_PR_ID = 4332
_PR_URL = f"https://github.com/{_SLUG}/pull/{_PR_ID}"


def _held_ticket(*, plan_minutes_ago: int | None = 4000, state: str = Ticket.State.SHIPPED) -> Ticket:
    ticket = Ticket.objects.create(
        role=Ticket.Role.AUTHOR,
        state=state,
        issue_url="https://github.com/acme/widgets/issues/797",
        overlay="t3-teatree",
    )
    PullRequest.objects.create(ticket=ticket, url=_PR_URL, repo=_SLUG, iid=str(_PR_ID))
    if plan_minutes_ago is not None:
        artifact = PlanArtifact.record(ticket=ticket, plan_text="the original plan", recorded_by="planning")
        PlanArtifact.objects.filter(pk=artifact.pk).update(
            recorded_at=timezone.now() - timedelta(minutes=plan_minutes_ago)
        )
    ReviewVerdict.record(
        pr_id=_PR_ID,
        slug=_SLUG,
        reviewed_sha=_SHA,
        verdict=ReviewVerdict.Verdict.HOLD,
        reviewer_identity="cold-reviewer",
        findings=[Finding(severity="blocker", summary="the fd/cwd half still fails open", file="probe.py", line=88)],
        ticket=ticket,
    )
    return ticket


class TestRouteRefixToPlanning(TestCase):
    def test_a_held_ticket_gets_one_planning_task_carrying_the_defect_class_brief(self) -> None:
        ticket = _held_ticket()

        assert route_refix_to_planning() == 1

        planning = Task.objects.filter(ticket=ticket, phase="planning")
        assert planning.count() == 1
        reason = planning.get().execution_reason
        assert "defect CLASS" in reason
        assert "EVERY site" in reason
        assert "the fd/cwd half still fails open" in reason

    def test_the_sweep_is_idempotent(self) -> None:
        """A second tick must not mint a rival planning task."""
        ticket = _held_ticket()
        route_refix_to_planning()

        assert route_refix_to_planning() == 0
        assert Task.objects.filter(ticket=ticket, phase="planning").count() == 1

    def test_a_replanned_ticket_is_not_routed(self) -> None:
        """The must-NOT-fire direction — a plan newer than the HOLD needs no re-planning."""
        ticket = _held_ticket(plan_minutes_ago=None)
        PlanArtifact.record(ticket=ticket, plan_text="re-planned after the hold", recorded_by="planning")

        assert route_refix_to_planning() == 0
        assert not Task.objects.filter(ticket=ticket, phase="planning").exists()

    def test_an_unheld_ticket_is_not_routed(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.PLANNED, overlay="t3-teatree")
        PlanArtifact.record(ticket=ticket, plan_text="the plan", recorded_by="planning")

        assert route_refix_to_planning() == 0
        assert not Task.objects.filter(ticket=ticket, phase="planning").exists()

    def test_the_sweep_runs_in_the_tick_recovery_chain(self) -> None:
        """The wiring, not the logic: an unwired sweep is a sweep that never runs."""
        ticket = _held_ticket()

        _reap_stale_task_claims(errors={})

        assert Task.objects.filter(ticket=ticket, phase="planning").exists()


class TestScheduleCodingIsRefused(TestCase):
    """The FSM half — ``schedule_coding`` runs the plan-currency gate, which now sees the HOLD."""

    def test_scheduling_coding_on_a_held_ticket_is_refused(self) -> None:
        ticket = _held_ticket(state=Ticket.State.PLANNED)

        with pytest.raises(NoCurrentPlanError, match="plan-reaffirm"):
            ticket.schedule_coding()

    def test_scheduling_coding_after_a_fresh_plan_succeeds(self) -> None:
        """The mirror: re-planning releases the refusal with no other change."""
        ticket = _held_ticket(state=Ticket.State.PLANNED)
        PlanArtifact.record(ticket=ticket, plan_text="re-planned after the hold", recorded_by="planning")

        task = ticket.schedule_coding()

        assert task.phase == "coding"

    def test_an_unheld_ticket_schedules_coding_unchanged(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.PLANNED, overlay="t3-teatree")
        PlanArtifact.record(ticket=ticket, plan_text="the plan", recorded_by="planning")

        assert ticket.schedule_coding().phase == "coding"


class TestClaimNextPendingRefusesTheUnplannedRefix(TestCase):
    """The claim half — the issue's named RED: the implementing task is not claimable."""

    def _pending_coding(self, ticket: Ticket) -> Task:
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        return Task.objects.create(
            ticket=ticket, session=session, phase="coding", execution_reason="re-fix the findings"
        )

    def test_the_unplanned_refix_is_not_claimed(self) -> None:
        ticket = _held_ticket()
        self._pending_coding(ticket)

        claimed = Task.objects.claim_next_pending(claimed_by="loop-slot", extra_filter=claimable_dispatch_q())

        assert claimed is None

    def test_the_same_task_is_claimed_once_the_plan_is_re_recorded(self) -> None:
        ticket = _held_ticket()
        task = self._pending_coding(ticket)
        PlanArtifact.record(ticket=ticket, plan_text="re-planned after the hold", recorded_by="planning")

        claimed = Task.objects.claim_next_pending(claimed_by="loop-slot", extra_filter=claimable_dispatch_q())

        assert claimed is not None
        assert claimed.pk == task.pk

    def test_an_unheld_tickets_coding_task_is_still_claimed(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.PLANNED, overlay="t3-teatree")
        PlanArtifact.record(ticket=ticket, plan_text="the plan", recorded_by="planning")
        task = self._pending_coding(ticket)

        claimed = Task.objects.claim_next_pending(claimed_by="loop-slot", extra_filter=claimable_dispatch_q())

        assert claimed is not None
        assert claimed.pk == task.pk

    def test_the_plain_dispatchable_set_is_unchanged(self) -> None:
        """``dispatchable_q`` stays the in-flight COUNTING set — narrowing it would over-admit."""
        ticket = _held_ticket()
        task = self._pending_coding(ticket)

        assert task.pk in set(Task.objects.filter(Task.dispatchable_q()).values_list("pk", flat=True))
