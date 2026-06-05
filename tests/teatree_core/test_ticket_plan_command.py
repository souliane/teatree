"""`t3 ticket plan` / `plan-bypass` — the operator plan-recorder CLI (#1977).

The plan-gate ``NoPlanArtifactError`` message names
``t3 <overlay> ticket plan <id> "<text>"`` as the escape, but that command did
not exist on the CLI and ``plan-bypass`` (which DID exist on ``manage.py
ticket``) was not wired into the ``t3 <overlay> ticket`` group. These tests
prove both commands now exist and do what the message promises.
"""

from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Ticket
from teatree.core.models.plan_artifact import PlanArtifact

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


def _started_ticket() -> Ticket:
    return Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)


class TicketPlanCommandTest(TestCase):
    def test_plan_records_artifact_and_advances_to_planned(self) -> None:
        ticket = _started_ticket()
        result = cast(
            "dict[str, object]",
            call_command("ticket", "plan", str(ticket.pk), "Step 1: do X. Step 2: do Y."),
        )
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.PLANNED
        assert PlanArtifact.objects.filter(ticket=ticket).count() == 1
        assert result["state"] == Ticket.State.PLANNED
        assert result["artifact_id"]

    def test_plan_with_blank_text_is_refused_and_records_nothing(self) -> None:
        ticket = _started_ticket()
        with pytest.raises(SystemExit):
            call_command("ticket", "plan", str(ticket.pk), "   ")
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED
        assert not PlanArtifact.objects.filter(ticket=ticket).exists()

    def test_plan_unknown_ticket_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command("ticket", "plan", "999999", "a plan")

    def test_plan_records_author_identity(self) -> None:
        ticket = _started_ticket()
        call_command("ticket", "plan", str(ticket.pk), "do the thing", "--recorded-by", "souliane")
        artifact = PlanArtifact.objects.filter(ticket=ticket).first()
        assert artifact is not None
        assert artifact.recorded_by == "souliane"

    def test_plan_on_non_started_ticket_returns_error_not_crash(self) -> None:
        # plan() is sourced only from STARTED; running it on a CODED ticket
        # surfaces the transition refusal as a structured error, not a crash.
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.CODED)
        result = cast("dict[str, object]", call_command("ticket", "plan", str(ticket.pk), "a plan"))
        assert result.get("error")
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.CODED


class TicketPlanBypassCommandTest(TestCase):
    def test_plan_bypass_records_audited_artifact_and_advances(self) -> None:
        ticket = _started_ticket()
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "plan-bypass",
                str(ticket.pk),
                "--human-authorize",
                "souliane",
                "--reason",
                "user-ordered ASAP fix",
            ),
        )
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.PLANNED
        artifact = PlanArtifact.objects.filter(ticket=ticket).first()
        assert artifact is not None
        assert "souliane" in artifact.recorded_by or artifact.recorded_by == "souliane"
        assert result["state"] == Ticket.State.PLANNED
