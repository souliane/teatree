"""Plan-before-code gate: single chokepoint for the PLANNED FSM state.

``check_plan_artifact`` is the ONE function called by ``Ticket.plan()`` via
``@transition(..., conditions=[check_plan_artifact])``.  No other code path
gates the plan() transition — all enforcement is structural (FSM source/target)
rather than scattered ad-hoc checks.

Mirrors dod_gate.py: a standalone module with a single check function so the
gate has one non-bypassable chokepoint and is independently testable.
"""

from typing import TYPE_CHECKING

from teatree.core.models.errors import NoPlanArtifactError

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


def check_plan_artifact(ticket: "Ticket") -> bool:
    """Return True iff at least one PlanArtifact exists for *ticket*.

    Used as a django-fsm ``@transition`` condition — must return a bool.
    Raises NoPlanArtifactError (an InvalidTransitionError subclass) on
    failure so callers get a typed exception with a diagnostic message,
    not a bare TransitionNotAllowed.

    The check is a simple existence query: ANY artifact row satisfies the
    gate.  The latest artifact governs; the append-only model preserves
    prior versions as an audit trail.
    """
    from teatree.core.models.plan_artifact import PlanArtifact  # noqa: PLC0415

    if PlanArtifact.objects.filter(ticket=ticket).exists():
        return True
    msg = (
        f"Ticket {ticket.pk} has no PlanArtifact — plan() requires a recorded "
        f"plan before the STARTED→PLANNED transition can fire. "
        f'Record a plan with `t3 <overlay> ticket plan <id> "<text>"` or let '
        f"the planner agent complete its task first."
    )
    raise NoPlanArtifactError(msg)
