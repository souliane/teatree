"""Plan-before-dispatch gate: no implementing agent is spawned on an unplanned ticket (#4409).

Planning was documented as mandatory, re-delivered to every attended session on a
cadence, and enforced at no seam a dispatch passes through.
:func:`~teatree.core.gates.plan_gate.check_plan_artifact` guards exactly one FSM
edge (``STARTED → PLANNED``), and every synthetic corrective re-entry mints a
coding / testing / debugging ``Task`` directly — so that edge is never taken and
the absence is never seen. The currency sibling declines the case by name:
:func:`~teatree.core.gates.plan_currency_gate.check_plan_current` returns True on
``artifact is None`` because absence "is the plan-first gate's concern, not this
one's", and it is behind the default-OFF ``require_plan_adequacy`` besides.

This closes absence at the seam a headless dispatch cannot avoid, and it is
deliberately unconditional — a gate behind a flag is the reminder it replaces.

The satisfying signals are exactly the two ``check_plan_artifact`` accepts, so the
two gates can never disagree about what "planned" means: any ``PlanArtifact`` row,
or a well-formed trivial-skip marker read through ``is_trivial_plan_skip`` (a
malformed one is absent, never a silent skip).

What is closed is the UNRECORDED skip, not the fast path. One
``ticket skip-planning`` records the decision and the dispatch proceeds, so the
refusal costs an operator seconds — the audit trail is the deliverable, not a
document.
"""

from typing import TYPE_CHECKING

from teatree.core.modelkit.phases import SUBAGENT_BY_PHASE, normalize_phase

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

#: The sub-agents that AUTHOR source. Deliberately not every dispatched agent: the
#: read-only and coordinating ones (``t3:reviewer``, ``t3:planner``, ``t3:shipper``)
#: write no implementation, so gating them would refuse the very work that RECORDS
#: the plan this gate wants.
IMPLEMENTING_SUBAGENTS: frozenset[str] = frozenset({"t3:coder", "t3:debugger", "t3:tester", "t3:e2e"})

#: Derived from the dispatch map rather than hand-listed, so any newly-routed
#: implementing phase is covered with no edit here — and the two cannot drift.
SUBAGENT_BY_IMPLEMENTING_PHASE: dict[str, str] = {
    phase: subagent for (_role, phase), subagent in SUBAGENT_BY_PHASE.items() if subagent in IMPLEMENTING_SUBAGENTS
}

IMPLEMENTING_PHASES: frozenset[str] = frozenset(SUBAGENT_BY_IMPLEMENTING_PHASE)

#: Greppable marker leading every refusal, matching the ``budget_exceeded:`` sibling
#: recorded at the same pre-harness seam.
PLAN_MISSING_PREFIX = "plan_missing: "


def unplanned_dispatch_refusal(ticket: "Ticket", *, phase: str) -> str | None:
    """The refusal for an implementing dispatch with no recorded plan decision, else ``None``.

    ``None`` for every non-implementing phase and for any ticket carrying either
    satisfying signal, so a dispatch is byte-identical to today unless it is an
    implementing one on a ticket where nobody recorded a decision about scope.

    Returns a reason rather than raising, mirroring
    :func:`~teatree.agents.spawn_payload.spawn_refusal_reason`: the caller records
    it as the attempt's own failure text, so the refusal is durable and names both
    remedies to whoever reads the card.
    """
    from teatree.core.models.plan_artifact import PlanArtifact  # noqa: PLC0415 — deferred: ORM/app-registry
    from teatree.core.models.trivial_plan_skip import is_trivial_plan_skip  # noqa: PLC0415 — deferred: ORM/app-registry

    canonical = normalize_phase(phase)
    if canonical not in IMPLEMENTING_PHASES:
        return None
    if PlanArtifact.objects.filter(ticket=ticket).exists():
        return None
    if is_trivial_plan_skip(ticket):
        return None
    return _refusal(ticket, canonical)


def _refusal(ticket: "Ticket", canonical_phase: str) -> str:
    return (
        f"{PLAN_MISSING_PREFIX}refusing to dispatch {SUBAGENT_BY_IMPLEMENTING_PHASE[canonical_phase]} for ticket "
        f"{ticket.pk} ({canonical_phase}) — it has no PlanArtifact and no recorded skip-planning, so no decision "
        f"about scope was recorded before code would be written. A ticket description, acceptance criteria, review "
        f"findings and a red CI log are NOT a plan. Record the plan with "
        f'`t3 <overlay> ticket plan {ticket.pk} "<text>"` — one or two sentences is a plan for a one-line fix — or, '
        f"for a trivial mechanical edit, record the skip with `t3 <overlay> ticket skip-planning {ticket.pk} "
        f'--reason "<why>"`.'
    )
