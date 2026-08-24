"""Drain the tickets the plan gate refused before intake learned to schedule planning (#4578).

Intake used to schedule ``coding`` on a freshly-admitted issue, the plan-before-dispatch
gate (#4409) refused it for carrying no ``PlanArtifact``, and nothing scheduled the
planning that would satisfy the refusal — so the ticket sat at NOT_STARTED with one
FAILED coding task, forever. ``persistence._handle_orchestrator`` now schedules planning,
which fixes every FUTURE admission and none of the 46 already stranded when it landed.

This sweep is the drain, and it reaches them off the failure's NAME rather than its text:
``FailureKind.PLAN_MISSING`` is precisely what the refusal is, and classifying it is what
lets a recovery mechanism see it. Once routed the ticket is STARTED, so the existing
``stuck_ticket_redispatch`` sweep owns it from there — including its repair budget and
its loud escalation — and this module needs neither.

The candidate set is deliberately narrow. ``stuck_ticket_redispatch._STATE_PHASE``
excludes the early states, and widening IT would be the cheaper-looking fix: it would
also hand a planning task to each per-overlay cadence placeholder ticket
(``scanning-news://``, ``eval-local://``, …), which sits at NOT_STARTED by design and has
no planning to do. Evidence of a refused implementing dispatch is what separates a
stranded ticket from a placeholder, so that evidence is the predicate.

Lives in ``teatree.loop`` (orchestration): it composes a ``core`` FSM method with a
``core.gates`` predicate over a housekeeping sweep, exactly as its sibling does.
"""

import logging

from teatree.core.gates.plan_dispatch_gate import unplanned_dispatch_refusal
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Task, Ticket

logger = logging.getLogger(__name__)

#: The states a ticket the plan gate refused is stranded in. Its intake never advanced it,
#: so it never reached the STARTED rung ``stuck_ticket_redispatch`` picks tickets up at.
_STRANDED_STATES: tuple[str, ...] = (Ticket.State.NOT_STARTED, Ticket.State.SCOPED)


def redispatch_unplanned_tickets() -> int:
    """Route each plan-gate-stranded ticket onto the planning path. Returns the count routed."""
    routed = 0
    for ticket in _stranded_candidates():
        # Per-item fault isolation (#3441): one poison row must not strand every other.
        try:
            ticket.begin_planning()
        except Exception:
            logger.exception("Unplanned-ticket redispatch skipped ticket %s", ticket.pk)
            continue
        logger.info("Ticket %s routed to planning after a plan_missing refusal", ticket.pk)
        routed += 1
    return routed


def _stranded_candidates() -> list[Ticket]:
    """Early-state author tickets whose last implementing dispatch was refused for a missing plan.

    Three narrowings, each load-bearing. Nothing in flight, so a ticket already being
    worked is left alone. A FAILED task carrying ``PLAN_MISSING``, which is the evidence
    a placeholder ticket can never have. And a LIVE re-read of the gate, so a ticket
    planned by hand since the refusal — or one carrying a ``skip-planning`` marker — is
    not dragged back through a phase it no longer needs.
    """
    stranded = (
        Ticket.objects.filter(role=Ticket.Role.AUTHOR, state__in=_STRANDED_STATES)
        .filter(tasks__status=Task.Status.FAILED, tasks__failure_kind=FailureKind.PLAN_MISSING)
        .exclude(tasks__status__in=Task.Status.active())
        .distinct()
    )
    return [t for t in stranded if unplanned_dispatch_refusal(t, phase="coding") is not None]
