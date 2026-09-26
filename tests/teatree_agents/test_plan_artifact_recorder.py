"""A planning envelope's plan lands its rubric on the GATED ticket, never a reviewer ticket (#4832).

``PlanArtifact.record`` writes ``acceptance_criteria`` onto whatever ``ticket`` the caller
passes. A planning task dispatched against a REVIEWER-role ticket (PR-keyed by ``issue_url``)
must route through :func:`resolve_gated_ticket` — the same resolver the merge gate and the
reviewing recorder already use — so the checklist lands where the done-gate actually reads it.
"""

from django.test import TestCase

from teatree.agents.plan_artifact_recorder import record_returned_plan
from teatree.core.models import PullRequest, Rubric, Session, Task, Ticket

_BASE_SHA = "a" * 40
_CRITERION = "the shipped default is claude-opus-5-5"
_ADEQUACY = {
    "design": {"content": "swap the shipped model constant"},
    "integration_seams": {"content": ["model_tiering.TIER_MODELS"]},
    "edge_cases": {"none_reason": "a config-value swap has no edge cases"},
    "test_strategy": {"content": "assert the constant's new value"},
    "acceptance_criteria": {"content": [_CRITERION]},
}


def _planning_task(ticket: Ticket) -> Task:
    session = Session.objects.create(ticket=ticket, agent_id="planning")
    task = Task.objects.create(ticket=ticket, session=session, phase="planning")
    task.claim(claimed_by="loop-slot")
    return task


def _envelope() -> dict[str, object]:
    return {
        "summary": "planned",
        "plan_text": "## Plan\n\nDo the thing.",
        "base_sha": _BASE_SHA,
        "adequacy": dict(_ADEQUACY),
    }


class TestOrdinaryTicket(TestCase):
    """An issue-keyed (non-reviewer) ticket is unaffected — the plan lands on itself."""

    def test_plan_records_on_the_planning_tickets_own_rubric(self) -> None:
        ticket = Ticket.objects.create(
            overlay="acme",
            role=Ticket.Role.AUTHOR,
            state=Ticket.State.WORK_STARTED,
            issue_url="https://github.com/acme/widgets/issues/9",
        )
        task = _planning_task(ticket)
        error = record_returned_plan(task, _envelope(), phase="planning")
        assert error == ""
        rubric = Rubric.objects.active_for_ticket(ticket)
        assert rubric is not None
        assert [c.text for c in rubric.criteria.all()] == [_CRITERION]


class TestReviewerTicketRoutesToTheGatedTicket(TestCase):
    """A plan recorded on a PR-keyed reviewer ticket lands its criteria on the GATED ticket."""

    def test_criteria_land_on_the_gated_ticket_not_the_reviewer_ticket(self) -> None:
        gated = Ticket.objects.create(
            overlay="acme",
            role=Ticket.Role.AUTHOR,
            state=Ticket.State.REVIEW_REQUESTED,
            issue_url="https://github.com/acme/widgets/issues/9",
        )
        PullRequest.objects.create(
            ticket=gated,
            repo="acme/widgets",
            iid="42",
            url="https://github.com/acme/widgets/pull/42",
        )
        reviewer_ticket = Ticket.objects.create(
            overlay="acme",
            role=Ticket.Role.REVIEWER,
            state=Ticket.State.WORK_STARTED,
            issue_url="https://github.com/acme/widgets/pull/42",
        )
        task = _planning_task(reviewer_ticket)
        error = record_returned_plan(task, _envelope(), phase="planning")
        assert error == ""
        assert Rubric.objects.active_for_ticket(reviewer_ticket) is None
        rubric = Rubric.objects.active_for_ticket(gated)
        assert rubric is not None
        assert [c.text for c in rubric.criteria.all()] == [_CRITERION]

    def test_an_unresolvable_gated_ticket_refuses_rather_than_recording_on_the_reviewer_ticket(self) -> None:
        # No PullRequest / MergeClear row names this PR's ticket at all.
        reviewer_ticket = Ticket.objects.create(
            overlay="acme",
            role=Ticket.Role.REVIEWER,
            state=Ticket.State.WORK_STARTED,
            issue_url="https://github.com/acme/widgets/pull/999",
        )
        task = _planning_task(reviewer_ticket)
        error = record_returned_plan(task, _envelope(), phase="planning")
        assert "gated ticket could not be resolved" in error
        assert Rubric.objects.active_for_ticket(reviewer_ticket) is None


class TestNonPlanningPhase(TestCase):
    def test_a_non_planning_phase_is_a_no_op(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        task.claim(claimed_by="loop-slot")
        assert record_returned_plan(task, _envelope(), phase="coding") == ""
        assert Rubric.objects.active_for_ticket(ticket) is None
