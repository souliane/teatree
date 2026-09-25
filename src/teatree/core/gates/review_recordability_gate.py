"""Review-recordability gate: a verdict that could bind to no tree is refused unpaid.

``record_returned_review_envelope`` already refuses a verdict whose pull request has no
recorded head — correctly, because such a verdict binds to nothing and no merge guard could
ever read it. It refuses AFTER the review has run: 53 refusals over eight days each paid for
a full Opus review whose verdict was then discarded, 29.8% of one window's metered spend.

The precondition is two pure DB reads on rows that exist before the task is claimed, so the
answer at dispatch time IS the answer at record time. This module is that same question,
asked first.

It calls :func:`~teatree.core.models.review_target.review_target_for_task` rather than
re-deriving the head, which is the whole point: with one resolver the pre-check and the
post-check cannot disagree about which PR and which head the review is answerable for.

Unconditional by design — a verdict that binds to no tree is unrecordable on every overlay,
on every forge, in every deployment, so a toggle would be surface paid at every reader with
exactly one possible value.

FAIL FAST, never park. ``Task.park`` is for a time-recoverable window that re-arms itself;
no amount of waiting stamps a head, so parking would invent a wake-up condition that does
not exist. The refusal is stateless, so the first tick after any producer supplies the head
dispatches normally.
"""

from typing import TYPE_CHECKING

from teatree.core.modelkit.phase_tools import ENVELOPE_VERDICT_PHASES
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.modelkit.task_failure_taxonomy import REVIEW_UNRECORDABLE_PREFIX
from teatree.core.models.review_target import ReviewTarget, review_target_for_task, review_target_for_ticket

if TYPE_CHECKING:
    from teatree.core.models.task import Task
    from teatree.core.models.ticket import Ticket


def unrecordable_review_refusal(task: "Task", *, phase: str) -> str | None:
    """Why *task*'s review must not be dispatched at all, or ``None`` to proceed.

    ``None`` for every phase outside :data:`ENVELOPE_VERDICT_PHASES`, and for a task
    answerable for NO pull request — an author-role reviewing task keyed by an issue URL is
    a self-review with no merge guard behind it, so it keeps today's completion byte for
    byte, exactly as the recorder documents.
    """
    if normalize_phase(phase) not in ENVELOPE_VERDICT_PHASES:
        return None
    return _refusal_for(review_target_for_task(task))


def unrecordable_review_mint_refusal(ticket: "Ticket") -> str | None:
    """Why no reviewing task may be MINTED for *ticket*, or ``None`` to mint it.

    The producer-facing half: the mint seams re-emit every tick and carry no failure memory,
    so a refused dispatch is re-minted as a brand-new task rather than retried. At mint time
    no dispatch row exists yet, so the ticket half of the resolver is the whole answer.
    """
    return _refusal_for(review_target_for_ticket(ticket))


def _refusal_for(target: ReviewTarget | None) -> str | None:
    if target is None or target.head_sha:
        return None
    return (
        f"{REVIEW_UNRECORDABLE_PREFIX}refusing to dispatch the reviewer for {target.slug}#{target.pr_id} — "
        f"no pull request head is recorded for it, so any verdict this review produced would bind to no tree "
        f"and no merge guard could ever read it. The review is not run, and nothing is marked reviewed. This "
        f"clears by itself as soon as any producer supplies the head (a push, or a re-broadcast whose forge "
        f"read succeeds); to stop asking, close the reviewer ticket."
    )
