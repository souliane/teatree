"""The drain for tickets the plan gate refused before intake scheduled planning (#4578).

Intake scheduled ``coding`` on a freshly-admitted issue, the plan-before-dispatch gate
refused it for carrying no ``PlanArtifact``, and nothing scheduled the planning that
would satisfy the refusal — so the ticket sat at NOT_STARTED with one FAILED coding
task forever. Fixing intake fixes every FUTURE admission and none of the already
stranded ones; this sweep is what reaches those, keyed on the refusal's NAME.
"""

import importlib

from django.apps import apps
from django.test import TestCase

from teatree.core.gates.plan_dispatch_gate import PLAN_MISSING_PREFIX
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Session, Task, Ticket
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.trivial_plan_skip import mark_trivial_plan_skip
from teatree.loop.tick_recovery import _reap_stale_task_claims
from teatree.loop.unplanned_ticket_redispatch import redispatch_unplanned_tickets

# A migration module name starts with a digit, so it is unreachable by import syntax.
_backfill = importlib.import_module("teatree.core.migrations.0081_plan_missing_failure_kind")
_rename_unclassified_plan_refusals = _backfill._rename_unclassified_plan_refusals


def _refused_ticket(
    *,
    state: str = Ticket.State.NOT_STARTED,
    role: str = Ticket.Role.AUTHOR,
    phase: str = "coding",
    status: str = Task.Status.FAILED,
) -> Ticket:
    """A ticket whose implementing dispatch was refused for a missing plan."""
    ticket = Ticket.objects.create(role=role, state=state, overlay="acme")
    session = Session.objects.create(ticket=ticket, agent_id=phase)
    task = Task.objects.create(ticket=ticket, session=session, phase=phase, subject="refused")
    if status == Task.Status.FAILED:
        task.fail(reason=f"{PLAN_MISSING_PREFIX}refusing to dispatch t3:coder for ticket {ticket.pk} ({phase})")
    else:
        Task.objects.filter(pk=task.pk).update(status=status)
    return ticket


def _planning_tasks(ticket: Ticket) -> list[Task]:
    return list(Task.objects.filter(ticket=ticket, phase="planning"))


class TestTheStrandDrains(TestCase):
    def test_a_refused_ticket_is_routed_to_planning(self) -> None:
        ticket = _refused_ticket()

        assert redispatch_unplanned_tickets() == 1

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED
        assert len(_planning_tasks(ticket)) == 1

    def test_a_scoped_ticket_is_reached_too(self) -> None:
        """SCOPED is the other rung intake could leave a ticket on below STARTED."""
        ticket = _refused_ticket(state=Ticket.State.SCOPED)

        assert redispatch_unplanned_tickets() == 1

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED

    def test_a_second_pass_mints_nothing(self) -> None:
        ticket = _refused_ticket()
        redispatch_unplanned_tickets()

        assert redispatch_unplanned_tickets() == 0
        assert len(_planning_tasks(ticket)) == 1

    def test_one_poison_row_does_not_strand_the_others(self) -> None:
        """A reviewer-role row is refused by ``schedule_planning``; the author row still drains."""
        _refused_ticket(role=Ticket.Role.REVIEWER)
        survivor = _refused_ticket()

        assert redispatch_unplanned_tickets() == 1
        assert len(_planning_tasks(survivor)) == 1

    def test_the_sweep_runs_inside_the_tick_recovery_chain(self) -> None:
        ticket = _refused_ticket()

        _reap_stale_task_claims({})

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED


class TestTheAlreadyStrandedTicketsAreReached(TestCase):
    """A row that failed BEFORE the kind existed is invisible to the sweep.

    It carries ``unclassified``, so migration 0081 must re-derive the name first — fixing
    intake alone leaves the already-stranded population dead.
    """

    def _stranded_under_the_old_vocabulary(self) -> Ticket:
        ticket = _refused_ticket()
        Task.objects.filter(ticket=ticket).update(failure_kind=FailureKind.UNCLASSIFIED)
        return ticket

    def test_an_unmigrated_row_is_invisible_to_the_sweep(self) -> None:
        self._stranded_under_the_old_vocabulary()

        assert redispatch_unplanned_tickets() == 0

    def test_the_migration_renames_it_and_the_sweep_then_drains_it(self) -> None:
        ticket = self._stranded_under_the_old_vocabulary()

        _rename_unclassified_plan_refusals(apps, None)

        assert redispatch_unplanned_tickets() == 1
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED

    def test_the_migration_leaves_an_unrelated_unclassified_row_alone(self) -> None:
        ticket = _refused_ticket()
        Task.objects.filter(ticket=ticket).update(
            failure_kind=FailureKind.UNCLASSIFIED, failure_reason="AssertionError: expected 3 got 4"
        )

        _rename_unclassified_plan_refusals(apps, None)

        assert Task.objects.get(ticket=ticket).failure_kind == FailureKind.UNCLASSIFIED


class TestWhatTheSweepMustNotTouch(TestCase):
    def test_a_ticket_with_work_in_flight_is_left_alone(self) -> None:
        """A PENDING sibling means the ticket is already being worked."""
        ticket = _refused_ticket()
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        Task.objects.create(ticket=ticket, session=session, phase="coding", subject="in flight")

        assert redispatch_unplanned_tickets() == 0
        assert _planning_tasks(ticket) == []

    def test_a_ticket_planned_since_the_refusal_is_left_alone(self) -> None:
        """The gate is re-read LIVE, so a hand-planned ticket is not dragged back through planning."""
        ticket = _refused_ticket()
        PlanArtifact.record(ticket=ticket, plan_text="a plan recorded by hand", recorded_by="owner")

        assert redispatch_unplanned_tickets() == 0
        assert _planning_tasks(ticket) == []

    def test_a_ticket_carrying_a_skip_planning_marker_is_left_alone(self) -> None:
        """The gate's OTHER satisfying signal. The drain must never become auto-skip-planning."""
        ticket = _refused_ticket()
        mark_trivial_plan_skip(ticket, reason="a one-character typo fix", by="owner")

        assert redispatch_unplanned_tickets() == 0
        assert _planning_tasks(ticket) == []

    def test_a_cadence_placeholder_ticket_is_left_alone(self) -> None:
        """Per-overlay placeholders (``scanning-news://`` …) sit at NOT_STARTED by design.

        This is why the sweep keys on a ``plan_missing`` failure rather than on the state:
        widening ``stuck_ticket_redispatch._STATE_PHASE`` to the early states would hand
        each of these a planning task it has no plan to produce.
        """
        placeholder = Ticket.objects.create(
            role=Ticket.Role.AUTHOR, state=Ticket.State.NOT_STARTED, issue_url="scanning-news://acme", overlay="acme"
        )
        session = Session.objects.create(ticket=placeholder, agent_id="scanning_news")
        Task.objects.create(ticket=placeholder, session=session, phase="scanning_news", subject="cadence")

        assert redispatch_unplanned_tickets() == 0
        assert _planning_tasks(placeholder) == []

    def test_a_task_that_failed_for_another_reason_is_left_alone(self) -> None:
        """``stuck_ticket_redispatch`` owns every other failure; this sweep owns exactly one."""
        ticket = _refused_ticket()
        Task.objects.filter(ticket=ticket).update(failure_kind=FailureKind.HARNESS_CRASH)

        assert redispatch_unplanned_tickets() == 0
        assert _planning_tasks(ticket) == []

    def test_a_ticket_past_the_early_states_is_left_alone(self) -> None:
        ticket = _refused_ticket(state=Ticket.State.CODED)

        assert redispatch_unplanned_tickets() == 0
        assert _planning_tasks(ticket) == []
