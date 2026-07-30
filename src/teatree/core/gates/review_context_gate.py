"""Reviewing-phase deep-retrieval gate: a verdict from the diff alone is refused.

Reviewing carries the same responsibility as implementing. The hole this
forecloses: ``lifecycle visit-phase <id> reviewing`` records the
independent-review attestation even when the reviewer never retrieved the work
item, never followed the links in the MR description + ticket, and never
downloaded + analyzed the referenced documents (specs, design docs,
amortization / Tilgungsplan schedules, requirement docs). A diff-only verdict
checks that the code compiles, not that it matches the specified requirements
and business rules.

When a project opts in by setting ``require_review_context`` (per-overlay or
global ``[teatree]``), entering the ``reviewing`` phase is refused until a
durable ``review_context`` artifact attests the retrieval: the work item was
fetched from its source and at least one referenced document was downloaded +
analyzed against the diff.

Opt-in default
    ``require_review_context`` is ``False`` unless configured. With it unset the
    gate is a NO-OP — projects that do not require deep retrieval keep recording
    ``reviewing`` unchanged.

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

from teatree.config import get_effective_settings
from teatree.core.modelkit.gate_registry import register_gate
from teatree.core.models.types import ReviewContext

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


class ReviewContextError(RuntimeError):
    """A ``reviewing`` attestation lacked recorded referenced-context retrieval."""


def review_context_required(overlay: str | None = None) -> bool:
    """Whether the deep-retrieval gate is in force for *overlay* (overlay -> global).

    *overlay* threads the ticket's own overlay so a per-overlay opt-in binds even
    when the evaluating process has no ambient ``T3_OVERLAY_NAME``. ``None``
    resolves the ambient overlay as before.
    """
    return get_effective_settings(overlay).require_review_context


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
    work_item = str(context.get("work_item", "")).strip()
    documents = context.get("documents") or []
    analysis = str(context.get("analysis", "")).strip()
    has_documents = isinstance(documents, list) and any(str(d).strip() for d in documents)
    return bool(work_item) and has_documents and bool(analysis)


def check_review_context(ticket: "Ticket") -> None:
    """Refuse a ``reviewing`` attestation that no deep-retrieval evidence backs.

    NO-OP when ``require_review_context`` is off (the opt-in default).
    Otherwise the durable ``review_context`` artifact must name the fetched
    work item, list a downloaded reference, and record its analysis.
    """
    if not review_context_required(ticket.overlay or None):
        return
    if is_complete(recorded_review_context(ticket)):
        return
    msg = (
        f"`lifecycle visit-phase {ticket.pk} reviewing` requires recorded "
        f"referenced-context retrieval (require_review_context): the work item "
        f"must be fetched from its source, every link in the MR description + "
        f"ticket followed, and each referenced document downloaded + analyzed "
        f"against the diff. Record it with `lifecycle record-review-context "
        f"{ticket.pk} --work-item <url> --document <url> --analysis <how-it-was-"
        f"checked>` once the retrieval is done, then retry."
    )
    raise ReviewContextError(msg)


def review_context_satisfied(ticket: "Ticket") -> bool:
    """Whether the ``-> reviewing`` deep-retrieval precondition is met (#2385).

    The boolean ``review()`` FSM condition. NO-OP (``True``) when the
    ``require_review_context`` knob is off, else true only when a complete
    ``review_context`` artifact is recorded.
    """
    if not review_context_required(ticket.overlay or None):
        return True
    return is_complete(recorded_review_context(ticket))


register_gate("review_context_satisfied", review_context_satisfied)
