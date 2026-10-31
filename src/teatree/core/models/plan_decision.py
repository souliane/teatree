"""Whether a ticket carries a recorded plan decision — the one answer every plan check reads (#4409).

Two signals satisfy it, and only these two: any ``PlanArtifact`` row, or a well-formed
trivial-skip marker (a malformed one is absent, never a silent skip). The ``plan()`` FSM
condition, the dispatch refusal, the fresh-mint refusal and the unplanned-redispatch sweep
all read :func:`has_plan_decision`, so none of them can disagree about what "planned" means.

What is closed is the UNRECORDED skip, not the fast path: one ``ticket skip-planning``
records the decision and every refusal below clears.

The sibling refusal for a conversation's bookkeeping ticket (#5685) sits here too, so the modules that mint
tasks read both mint refusals from one place.
"""

from typing import TYPE_CHECKING

from teatree.core.modelkit.phases import IMPLEMENTING_PHASES, SUBAGENT_BY_IMPLEMENTING_PHASE, normalize_phase
from teatree.core.models.errors import NoPlanArtifactError, TaskMintRefusedError
from teatree.core.models.trivial_plan_skip import is_trivial_plan_skip

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

#: Greppable marker leading every refusal, matching the ``budget_exceeded:`` sibling.
PLAN_MISSING_PREFIX = "plan_missing: "


def has_plan_decision(ticket: "Ticket") -> bool:
    from teatree.core.models.plan_artifact import PlanArtifact  # noqa: PLC0415 — plan_artifact imports Ticket

    return PlanArtifact.objects.filter(ticket=ticket).exists() or is_trivial_plan_skip(ticket)


def plan_missing_refusal(ticket: "Ticket", *, phase: str) -> str | None:
    """The refusal for an implementing *phase* on a ticket with no plan decision, else ``None``."""
    canonical = normalize_phase(phase)
    if canonical not in IMPLEMENTING_PHASES or has_plan_decision(ticket):
        return None
    return (
        f"{PLAN_MISSING_PREFIX}refusing to dispatch {SUBAGENT_BY_IMPLEMENTING_PHASE[canonical]} for ticket "
        f"{ticket.pk} ({canonical}) — it has no PlanArtifact and no recorded skip-planning, so no decision "
        f"about scope was recorded before code would be written. A ticket description, acceptance criteria, review "
        f"findings and a red CI log are NOT a plan. Record the plan with "
        f'`t3 <overlay> ticket plan {ticket.pk} "<text>"` — one or two sentences is a plan for a one-line fix — or, '
        f"for a trivial mechanical edit, record the skip with `t3 <overlay> ticket skip-planning {ticket.pk} "
        f'--reason "<why>"`.'
    )


def refuse_unplanned_mint(ticket: "Ticket", *, phase: str) -> None:
    refusal = plan_missing_refusal(ticket, phase=phase)
    if refusal is not None:
        raise NoPlanArtifactError(refusal)


CONVERSATION_REFUSED_PREFIX = "conversation_ticket: "

_BUILD_PHASES = IMPLEMENTING_PHASES | {"shipping"}


def conversation_refusal(ticket: "Ticket", *, phase: str) -> str | None:
    """The refusal for a build *phase* on a conversation ticket, else ``None``."""
    canonical = normalize_phase(phase)
    if canonical not in _BUILD_PHASES or not ticket.is_conversation():
        return None
    return (
        f"{CONVERSATION_REFUSED_PREFIX}refusing a {canonical} task on ticket {ticket.pk} — it is a "
        f"conversation, not a tracked work item, so nothing built on it could be found or shipped. File the "
        f"request as a work item first; the promotion moves the thread to that ticket, which takes the task."
    )


def refuse_conversation_mint(ticket: "Ticket", *, phase: str) -> None:
    refusal = conversation_refusal(ticket, phase=phase)
    if refusal is not None:
        raise TaskMintRefusedError(refusal)
