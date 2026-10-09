"""Reviewing-phase deep-retrieval gate: a verdict from the diff alone is refused.

Reviewing carries the same responsibility as implementing. The hole this
forecloses: ``lifecycle visit-phase <id> reviewing`` records the
independent-review attestation even when the reviewer never retrieved the work
item, never followed the links in the MR description + ticket, and never
downloaded + analyzed the referenced documents (specs, design docs,
amortization schedules, requirement docs). A diff-only verdict
checks that the code compiles, not that it matches the specified requirements
and business rules.

Entering the ``reviewing`` phase is refused until a
durable ``review_context`` artifact attests the retrieval: the work item was
fetched from its source and at least one referenced document was downloaded +
analyzed against the diff.

Satisfying evidence
    ``ticket.extra['review_context']`` whose ``work_item`` names the fetched
    source, ``documents`` lists at least one downloaded reference, and
    ``analysis`` records how the implementation was checked against it.

The gate is a pure function over durable ``extra`` state, mirroring
``teatree.core.gates.review_skill_gate``. On a block it raises
:class:`ReviewContextError` with a remediation message naming the
``record-review-context`` command; the ``visit-phase`` command surfaces it as a
non-zero exit.
"""

from typing import TYPE_CHECKING

from teatree.core.modelkit.gate_registry import register_gate
from teatree.core.models.types import ReviewContext

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


class ReviewContextError(RuntimeError):
    """A ``reviewing`` attestation lacked recorded referenced-context retrieval."""


def recorded_review_context(ticket: "Ticket") -> ReviewContext:
    """The recorded deep-retrieval evidence, or an empty mapping."""
    raw = (ticket.extra or {}).get("review_context") or {}
    return ReviewContext(**{k: v for k, v in raw.items() if k in ReviewContext.__annotations__})


def is_complete(context: ReviewContext) -> bool:
    """Whether a ``review_context`` records a real retrieval (not a stub).

    A genuine deep retrieval names the fetched work item, lists at least one
    downloaded reference document, and records how it was analyzed against the
    diff. An empty or partial record does not satisfy the gate — recording the
    artifact must mean the work was done, not merely that the command ran.
    """
    return not missing_review_context_fields(context)


def missing_review_context_fields(context: ReviewContext) -> list[str]:
    documents = context.get("documents") or []
    present = {
        "work_item": bool(str(context.get("work_item", "")).strip()),
        "documents": isinstance(documents, list) and any(str(d).strip() for d in documents),
        "analysis": bool(str(context.get("analysis", "")).strip()),
    }
    return [field for field, ok in present.items() if not ok]


def check_review_context(ticket: "Ticket") -> None:
    """Refuse a ``reviewing`` attestation that no deep-retrieval evidence backs.

    The durable ``review_context`` artifact must name the fetched
    work item, list a downloaded reference, and record its analysis.
    """
    if is_complete(recorded_review_context(ticket)):
        return
    msg = (
        f"`lifecycle visit-phase {ticket.pk} reviewing` requires recorded "
        f"referenced-context retrieval: the work item "
        f"must be fetched from its source, every link in the MR description + "
        f"ticket followed, and each referenced document downloaded + analyzed "
        f"against the diff. Record it with `lifecycle record-review-context "
        f"{ticket.pk} --work-item <url> --documents <urls> --analysis <how-it-was-"
        f"checked>` once the retrieval is done, then retry."
    )
    raise ReviewContextError(msg)


def review_context_satisfied(ticket: "Ticket") -> bool:
    """Whether the ``-> reviewing`` deep-retrieval precondition is met (#2385).

    The boolean ``review()`` FSM condition is true only when a complete
    ``review_context`` artifact is recorded.
    """
    return is_complete(recorded_review_context(ticket))


register_gate("review_context_satisfied", review_context_satisfied)
