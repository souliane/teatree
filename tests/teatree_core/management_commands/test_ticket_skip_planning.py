"""`t3 ticket skip-planning` — the lightweight, audited plan-gate carve-out.

The heavyweight ``plan-bypass`` (``--human-authorize``) records a fabricated
``PlanArtifact``; ``skip-planning`` is its lightweight sibling for a trivial
mechanical edit — it records a durable, audited ``trivial_plan_skip`` marker
(MANDATORY ``--reason``) and advances WORK_STARTED → PLAN_RECORDED with no PlanArtifact and
no human-authorize. A blank reason is refused.
"""

from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.gates.rubric_gate import (
    RubricNotSatisfiedError,
    RubricNotVerifiedError,
    check_rubric_satisfied,
    check_rubric_verified,
)
from teatree.core.models import Rubric, Session, Ticket
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.rubric import PHASE_CRITERIA
from teatree.core.models.trivial_plan_skip import is_trivial_plan_skip, trivial_plan_skip_reason

_SHA = "a" * 40

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


def _started_ticket() -> Ticket:
    return Ticket.objects.create(overlay="test", state=Ticket.State.WORK_STARTED)


class TicketSkipPlanningCommandTest(TestCase):
    def test_skip_planning_records_marker_and_advances_without_artifact(self) -> None:
        ticket = _started_ticket()
        result = cast(
            "dict[str, object]",
            call_command("ticket", "skip-planning", str(ticket.pk), "--reason", "one-line typo fix"),
        )
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.PLAN_RECORDED
        assert not PlanArtifact.objects.filter(ticket=ticket).exists()
        assert is_trivial_plan_skip(ticket) is True
        assert trivial_plan_skip_reason(ticket) == "one-line typo fix"
        assert result["state"] == Ticket.State.PLAN_RECORDED

    def test_skip_planning_with_blank_reason_is_refused_and_records_nothing(self) -> None:
        ticket = _started_ticket()
        with pytest.raises(SystemExit):
            call_command("ticket", "skip-planning", str(ticket.pk), "--reason", "   ")
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.WORK_STARTED
        assert is_trivial_plan_skip(ticket) is False

    def test_skip_planning_records_who_decided(self) -> None:
        ticket = _started_ticket()
        call_command("ticket", "skip-planning", str(ticket.pk), "--reason", "trivial", "--by", "souliane")
        ticket.refresh_from_db()
        assert ticket.extra["trivial_plan_skip"]["by"] == "souliane"

    def test_skip_planning_unknown_ticket_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command("ticket", "skip-planning", "999999", "--reason", "trivial")

    def test_skip_planning_on_non_started_ticket_still_records_the_signal(self) -> None:
        # plan() (WORK_STARTED -> PLAN_RECORDED) is sourced only from WORK_STARTED; for an
        # already-in-flight ticket (#4449 class), the gate's satisfying signal
        # is the trivial-skip marker's EXISTENCE, not the transition -- so the
        # marker is still recorded, with no transition attempted and no error
        # surfaced, and the ticket's state is left untouched.
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.CODED)
        result = cast(
            "dict[str, object]",
            call_command("ticket", "skip-planning", str(ticket.pk), "--reason", "trivial"),
        )
        assert not result.get("error")
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.CODED
        assert is_trivial_plan_skip(ticket) is True
        assert trivial_plan_skip_reason(ticket) == "trivial"


class TestSkipPlanningEscapesThePlanGateOnly(TestCase):
    """It clears the PLAN gate; the rubric done-gate still grades what the ticket carries.

    A trivial skip writes no ``PlanArtifact``, so the done-gate's one waiver — a
    bypass-shaped latest plan — is absent, while every visited phase with a
    ``PHASE_CRITERIA`` entry has already seeded an ungraded criterion. An unattended
    trivial edit therefore stops short of MERGED and DELIVERED, which is deliberate and
    is what the command's docstring promises.
    """

    def _skipped_after_coding(self) -> Ticket:
        ticket = _started_ticket()
        Session.objects.create(ticket=ticket).visit_phase("coding")
        call_command("ticket", "skip-planning", str(ticket.pk), "--reason", "one-line typo fix")
        ticket.refresh_from_db()
        return ticket

    def test_the_visited_phase_leaves_an_ungraded_criterion_behind(self) -> None:
        rubric = Rubric.objects.active_for_ticket(self._skipped_after_coding())
        assert rubric is not None
        assert [c.text for c in rubric.criteria.all()] == [PHASE_CRITERIA["coding"]]

    def test_the_merge_gate_still_refuses(self) -> None:
        with pytest.raises(RubricNotSatisfiedError, match="ungraded"):
            check_rubric_satisfied(self._skipped_after_coding(), _SHA, transition="merge")

    def test_the_delivered_gate_still_refuses(self) -> None:
        with pytest.raises(RubricNotVerifiedError, match="ungraded"):
            check_rubric_verified(self._skipped_after_coding())

    def test_the_audited_plan_bypass_is_what_clears_both(self) -> None:
        """The control: the skip waives nothing, and the human-authorized bypass does."""
        ticket = self._skipped_after_coding()

        call_command("ticket", "plan-bypass", str(ticket.pk), "--human-authorize", "alice", "--reason", "typo")

        assert check_rubric_satisfied(ticket, _SHA, transition="merge") is None
        assert check_rubric_verified(ticket) is None
