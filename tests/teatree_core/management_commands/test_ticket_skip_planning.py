"""`t3 ticket skip-planning` — the lightweight, audited plan-gate carve-out.

The heavyweight ``plan-bypass`` (``--human-authorize``) records a fabricated
``PlanArtifact``; ``skip-planning`` is its lightweight sibling for a trivial
mechanical edit — it records a durable, audited ``trivial_plan_skip`` marker
(MANDATORY ``--reason``) and advances STARTED → PLANNED with no PlanArtifact and
no human-authorize. A blank reason is refused.
"""

from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Ticket
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.trivial_plan_skip import is_trivial_plan_skip, trivial_plan_skip_reason

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


def _started_ticket() -> Ticket:
    return Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)


class TicketSkipPlanningCommandTest(TestCase):
    def test_skip_planning_records_marker_and_advances_without_artifact(self) -> None:
        ticket = _started_ticket()
        result = cast(
            "dict[str, object]",
            call_command("ticket", "skip-planning", str(ticket.pk), "--reason", "one-line typo fix"),
        )
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.PLANNED
        assert not PlanArtifact.objects.filter(ticket=ticket).exists()
        assert is_trivial_plan_skip(ticket) is True
        assert trivial_plan_skip_reason(ticket) == "one-line typo fix"
        assert result["state"] == Ticket.State.PLANNED

    def test_skip_planning_with_blank_reason_is_refused_and_records_nothing(self) -> None:
        ticket = _started_ticket()
        with pytest.raises(SystemExit):
            call_command("ticket", "skip-planning", str(ticket.pk), "--reason", "   ")
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED
        assert is_trivial_plan_skip(ticket) is False

    def test_skip_planning_records_who_decided(self) -> None:
        ticket = _started_ticket()
        call_command("ticket", "skip-planning", str(ticket.pk), "--reason", "trivial", "--by", "souliane")
        ticket.refresh_from_db()
        assert ticket.extra["trivial_plan_skip"]["by"] == "souliane"

    def test_skip_planning_unknown_ticket_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command("ticket", "skip-planning", "999999", "--reason", "trivial")

    def test_skip_planning_on_non_started_ticket_returns_error_not_crash(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.CODED)
        result = cast(
            "dict[str, object]",
            call_command("ticket", "skip-planning", str(ticket.pk), "--reason", "trivial"),
        )
        assert result.get("error")
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.CODED
