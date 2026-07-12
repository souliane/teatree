"""Plan-before-code gate: single chokepoint for the PLANNED FSM state.

``check_plan_artifact`` is the ONE function called by ``Ticket.plan()`` via
``@transition(..., conditions=[check_plan_artifact])``.  No other code path
gates the plan() transition — all enforcement is structural (FSM source/target)
rather than scattered ad-hoc checks.

Mirrors dod_gate.py: a standalone module with a single check function so the
gate has one non-bypassable chokepoint and is independently testable.
"""

from typing import TYPE_CHECKING

from teatree.core.modelkit.gate_registry import register_gate
from teatree.core.models.errors import NoPlanArtifactError

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


def check_plan_artifact(ticket: "Ticket") -> bool:
    """Return True iff *ticket* may legitimately leave STARTED for PLANNED.

    Used as a django-fsm ``@transition`` condition — must return a bool.
    Raises NoPlanArtifactError (an InvalidTransitionError subclass) on
    failure so callers get a typed exception with a diagnostic message,
    not a bare TransitionNotAllowed.

    Two satisfying signals, in order.

    First, at least one ``PlanArtifact`` exists — the normal planner /
    ``ticket plan`` / audited ``plan-bypass`` paths. ANY artifact row satisfies
    the gate; the latest governs, and the append-only model preserves prior
    versions as an audit trail.

    Second, a well-formed trivial-skip marker is recorded — the lightweight,
    audited carve-out for trivial mechanical edits (a typo, a one-line bump).
    Read through ``trivial_plan_skip.is_trivial_plan_skip`` so the marker's
    mandatory-reason / fail-safe-to-absent validation is the single source of
    truth; a malformed or empty-reason marker is treated as absent, so it never
    silently skips the gate.

    The carve-out is scoped to a ticket that explicitly carries the marker, so it
    cannot leak to an ordinary unmarked ticket.
    """
    from teatree.core.models.plan_artifact import PlanArtifact  # noqa: PLC0415 — deferred: ORM/app-registry
    from teatree.core.models.trivial_plan_skip import is_trivial_plan_skip  # noqa: PLC0415 — deferred: ORM/app-registry

    if PlanArtifact.objects.filter(ticket=ticket).exists():
        return True
    if is_trivial_plan_skip(ticket):
        return True
    msg = (
        f"Ticket {ticket.pk} has no PlanArtifact and no trivial-skip marker — "
        f"plan() requires one before the STARTED→PLANNED transition can fire. "
        f'Record a plan with `t3 <overlay> ticket plan <id> "<text>"`, let the '
        f"planner agent complete its task, or — for a trivial mechanical edit — "
        f'mark it with `t3 <overlay> ticket skip-planning <id> --reason "<why>"`.'
    )
    raise NoPlanArtifactError(msg)


register_gate("plan_artifact", check_plan_artifact)
