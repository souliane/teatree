"""`t3 ticket refix-plan-status` — the #4348 surface for the unplanned-re-fix gap.

The measurement that opened the issue was hand-queried out of the control DB
because nothing reported it. The report's two directions matter equally: a held
ticket whose plan predates its verdict appears WITH the detector value, and a
re-planned one leaves — a surface that always answers "nothing here" would look
identical to a healthy factory.
"""

import json
from datetime import timedelta
from io import StringIO
from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import PullRequest, Ticket
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.review_verdict import ReviewVerdict

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)

_SHA = "c1b2c3d4" * 5
_SLUG = "acme/widgets"
_PR_ID = 4306


def _held_ticket(*, stale_plan: bool) -> Ticket:
    ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR, state=Ticket.State.SHIPPED)
    PullRequest.objects.create(
        ticket=ticket, url=f"https://github.com/{_SLUG}/pull/{_PR_ID}", repo=_SLUG, iid=str(_PR_ID)
    )
    if stale_plan:
        artifact = PlanArtifact.record(ticket=ticket, plan_text="the original plan", recorded_by="planning")
        PlanArtifact.objects.filter(pk=artifact.pk).update(recorded_at=timezone.now() - timedelta(days=3))
    ReviewVerdict.record(
        pr_id=_PR_ID,
        slug=_SLUG,
        reviewed_sha=_SHA,
        verdict=ReviewVerdict.Verdict.HOLD,
        reviewer_identity="cold-reviewer",
        ticket=ticket,
    )
    return ticket


class TicketRefixPlanStatusCommandTest(TestCase):
    def test_reports_a_held_ticket_whose_plan_predates_its_verdict(self) -> None:
        ticket = _held_ticket(stale_plan=True)

        result = cast("dict[str, object]", call_command("ticket", "refix-plan-status", "--json"))

        awaiting = cast("list[dict[str, object]]", result["awaiting"])
        assert [row["ticket_id"] for row in awaiting] == [ticket.pk]
        assert awaiting[0]["plan_recorded_at"]

    def test_a_re_planned_ticket_leaves_the_report(self) -> None:
        ticket = _held_ticket(stale_plan=True)
        PlanArtifact.record(ticket=ticket, plan_text="re-planned after the hold", recorded_by="planning")

        result = cast("dict[str, object]", call_command("ticket", "refix-plan-status", "--json"))

        assert result["awaiting"] == []

    def test_a_ticket_with_no_plan_at_all_reports_an_empty_plan_stamp(self) -> None:
        _held_ticket(stale_plan=False)

        result = cast("dict[str, object]", call_command("ticket", "refix-plan-status", "--json"))

        awaiting = cast("list[dict[str, object]]", result["awaiting"])
        assert awaiting[0]["plan_recorded_at"] == ""

    def test_the_overlay_filter_scopes_the_report(self) -> None:
        ticket = _held_ticket(stale_plan=True)

        scoped = cast("dict[str, object]", call_command("ticket", "refix-plan-status", "--overlay", "test", "--json"))
        other = cast("dict[str, object]", call_command("ticket", "refix-plan-status", "--overlay", "nope", "--json"))

        assert [row["ticket_id"] for row in cast("list[dict[str, object]]", scoped["awaiting"])] == [ticket.pk]
        assert other["awaiting"] == []


class TicketRefixPlanStatusChannelTest(TestCase):
    """A front-end parses ``--json`` stdout, so it carries JSON or nothing at all."""

    @staticmethod
    def _channels(*args: str) -> tuple[str, str]:
        out, err = StringIO(), StringIO()
        call_command("ticket", "refix-plan-status", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_human_mode_leaves_stdout_empty(self) -> None:
        _held_ticket(stale_plan=True)

        out, err = self._channels()

        assert out == ""
        assert "Tickets awaiting a post-HOLD replan" in err

    def test_json_mode_puts_only_json_on_stdout(self) -> None:
        _held_ticket(stale_plan=True)

        out, err = self._channels("--json")

        assert json.loads(out)["awaiting"]
        assert err == ""
