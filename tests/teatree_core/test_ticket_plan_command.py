"""`t3 ticket plan` / `plan-bypass` — the operator plan-recorder CLI (#1977).

The plan-gate ``NoPlanArtifactError`` message names
``t3 <overlay> ticket plan <id> "<text>"`` as the escape, but that command did
not exist on the CLI and ``plan-bypass`` (which DID exist on ``manage.py
ticket``) was not wired into the ``t3 <overlay> ticket`` group. These tests
prove both commands now exist and do what the message promises.
"""

import json
from io import StringIO
from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Ticket
from teatree.core.models.plan_artifact import PlanArtifact
from tests.factories import _FORTY_HEX, TEST_ADEQUACY

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


def _started_ticket() -> Ticket:
    return Ticket.objects.create(overlay="test", state=Ticket.State.STARTED)


def _manifest_json() -> str:
    return json.dumps(TEST_ADEQUACY)


def _plan_args(ticket: Ticket, plan_text: str) -> tuple[str, ...]:
    """The full `ticket plan` argv — base_sha and the manifest are BOTH required."""
    return (
        "ticket",
        "plan",
        str(ticket.pk),
        plan_text,
        "--base-sha",
        _FORTY_HEX,
        "--adequacy-json",
        _manifest_json(),
    )


class TicketPlanCommandTest(TestCase):
    def test_plan_records_artifact_and_advances_to_planned(self) -> None:
        ticket = _started_ticket()
        result = cast(
            "dict[str, object]",
            call_command(*_plan_args(ticket, "Step 1: do X. Step 2: do Y.")),
        )
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.PLANNED
        assert PlanArtifact.objects.filter(ticket=ticket).count() == 1
        assert result["state"] == Ticket.State.PLANNED
        assert result["artifact_id"]

    def test_plan_with_blank_text_is_refused_and_records_nothing(self) -> None:
        ticket = _started_ticket()
        stderr = StringIO()
        with pytest.raises(SystemExit):
            call_command(*_plan_args(ticket, "   "), stderr=stderr)
        assert "plan_text is required" in stderr.getvalue()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED
        assert not PlanArtifact.objects.filter(ticket=ticket).exists()

    def test_plan_unknown_ticket_exits_nonzero(self) -> None:
        # The flags are supplied so the exit comes from resolving the ticket, not from
        # the missing-flag short-circuit that precedes it.
        stderr = StringIO()
        with pytest.raises(SystemExit):
            call_command(
                "ticket",
                "plan",
                "999999",
                "a plan",
                "--base-sha",
                _FORTY_HEX,
                "--adequacy-json",
                _manifest_json(),
                stderr=stderr,
            )
        assert "not found" in stderr.getvalue()

    def test_plan_without_the_manifest_is_refused_and_names_the_escapes(self) -> None:
        ticket = _started_ticket()
        stderr = StringIO()
        with pytest.raises(SystemExit):
            call_command("ticket", "plan", str(ticket.pk), "a plan", stderr=stderr)
        message = stderr.getvalue()
        assert "skip-planning" in message
        assert "plan-bypass" in message
        assert not PlanArtifact.objects.filter(ticket=ticket).exists()

    def test_plan_records_author_identity(self) -> None:
        ticket = _started_ticket()
        call_command(*_plan_args(ticket, "do the thing"), "--recorded-by", "souliane")
        artifact = PlanArtifact.objects.filter(ticket=ticket).first()
        assert artifact is not None
        assert artifact.recorded_by == "souliane"

    def test_plan_on_non_started_ticket_still_records_the_signal(self) -> None:
        # plan() (the STARTED -> PLANNED FSM transition) is sourced only from
        # STARTED; for an already-in-flight ticket (#4449 class), the gate's
        # satisfying signal is the PlanArtifact's EXISTENCE, not the transition
        # -- so the artifact is still recorded, with no transition attempted
        # and no error surfaced, and the ticket's state is left untouched.
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.CODED)
        result = cast("dict[str, object]", call_command(*_plan_args(ticket, "a plan")))
        assert not result.get("error")
        assert PlanArtifact.objects.filter(ticket=ticket).exists()
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

    def test_plan_bypass_on_non_started_ticket_still_records_the_signal(self) -> None:
        # Same #4449-class fix as `plan`: an already-in-flight (non-STARTED)
        # ticket still gets its audited bypass artifact recorded -- no
        # transition attempted, no error, state unchanged.
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.CODED)
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
        assert not result.get("error")
        artifact = PlanArtifact.objects.filter(ticket=ticket).first()
        assert artifact is not None
        assert "souliane" in artifact.recorded_by or artifact.recorded_by == "souliane"
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.CODED
