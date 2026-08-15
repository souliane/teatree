"""Route a held ticket's re-fix through planning, so the block is never a silent stall (#4348).

The claim-boundary and FSM teeth (``core.review.refix_plan``) refuse to implement a
ticket whose plan predates its newest HOLD verdict. Taken alone that converts one bad
outcome into a worse one: a held PR whose implementer never runs, with nothing queued
and no signal — the board still reads as progressing. This sweep is the other half. It
enqueues ONE planning task per blocked ticket, so the block clears itself the moment a
plan is recorded, and the re-fix reaches an implementing agent WITH a plan instead of
with a findings list.

The brief is the deliverable the old flow had no place for: the plan must state the
defect CLASS independently of any line number and enumerate every site in the touched
module that can exhibit it. Both PRs that produced the measurement fixed the line they
were handed and were held again for the same defect through a different door.

Runs in the tick-recovery sweep chain, per-item fault isolated like its siblings, and
idempotent through the same ``create_phase_task`` CAS: a ticket that already has a
planning task in flight is skipped rather than raced.
"""

import logging

from teatree.core.models import Ticket
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.core.review.refix_plan import newest_hold_verdict, tickets_awaiting_refix_plan
from teatree.loop.persistence_phase_task import create_phase_task, has_open_task

logger = logging.getLogger(__name__)

#: How many findings the brief quotes before it summarises the rest. The brief names
#: the verdict so the planner can read the whole list; the excerpt is orientation.
_BRIEF_FINDING_LIMIT = 5


def route_refix_to_planning() -> int:
    """Enqueue a planning task for each ticket awaiting a post-HOLD replan; return how many."""
    scheduled = 0
    for row in tickets_awaiting_refix_plan():
        try:
            scheduled += _route_one(row.ticket_id)
        except Exception:
            logger.exception("Post-HOLD replan routing skipped ticket %s after an unexpected error", row.ticket_id)
    return scheduled


def _route_one(ticket_id: int) -> int:
    ticket = Ticket.objects.filter(pk=ticket_id).first()
    if ticket is None or has_open_task(ticket, phase="planning"):
        return 0
    create_phase_task(ticket, phase="planning", agent_id="planning", reason=replan_reason(ticket))
    logger.info("Routed ticket %s through planning — its plan predates the newest review HOLD", ticket_id)
    return 1


def replan_reason(ticket: Ticket) -> str:
    """The planning task's brief: re-plan the re-fix, generalised past the findings."""
    verdict = newest_hold_verdict(ticket)
    location = f"{verdict.slug}#{verdict.pr_id}" if verdict is not None else "the held PR"
    return (
        f"Auto-scheduled re-planning — a review HOLD on {location} voided the plan this ticket's next "
        f"implementation would run under. A findings list is NOT a plan: state the defect CLASS "
        f"independently of any line number, enumerate EVERY site in the touched module and its callers "
        f"that can exhibit it, and adjudicate each one (fix / deliberately left, with the reason). For "
        f"each regression test, name what would make it fail and confirm it does fail on unfixed code. "
        f"{_findings_excerpt(verdict)}"
    )


def _findings_excerpt(verdict: ReviewVerdict | None) -> str:
    """The HOLD's findings as orientation, or a pointer to read them."""
    findings = verdict.structured_findings if verdict is not None else []
    if not findings:
        return "Read the recorded HOLD verdict's findings before planning."
    shown = findings[:_BRIEF_FINDING_LIMIT]
    lines = "; ".join(f"{finding.location()}: {finding.summary}" for finding in shown)
    more = f" (+{len(findings) - len(shown)} more)" if len(findings) > len(shown) else ""
    return f"The HOLD's findings are the SYMPTOMS to generalise from, not the work order: {lines}{more}."
