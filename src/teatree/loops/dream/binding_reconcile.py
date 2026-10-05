"""Two conflicting BINDING memories become ONE reconciliation gap on the umbrella's pending ledger (#2723).

Binding doctrine that disagrees is never auto-merged; a human decides which rule is
canonical. The pass files no forge issue for it: the pair is queued like every other gap,
the backlog sweep folds it into an existing host through ``ticket attach-gaps`` (where the
forge-write seam scrubs it), and that host records the decision as the gap's disposition.
"""

from typing import TYPE_CHECKING

from teatree.core.models.dream_gap_ledger import queue_pending, umbrella_ticket
from teatree.core.review.review_findings import neutralize_bare_references
from teatree.loops.dream.batch_promote import covering_ticket
from teatree.loops.dream.promote_memory import TicketOutcome
from teatree.loops.dream.umbrella_ledger import _withholding_reason

if TYPE_CHECKING:
    from collections.abc import Sequence

    from teatree.loops.dream.merge import BindingConflict


def _conflict_key(conflict: "BindingConflict") -> str:
    return "binding-reconcile-" + "+".join(sorted((conflict.survivor_name, conflict.absorbed_name)))


def queue_binding_reconciliations(
    *, umbrella_url: str, conflicts: "Sequence[BindingConflict]", dry_run: bool = False
) -> list[TicketOutcome]:
    """Queue one deduped reconciliation gap per conflicting pair; one outcome per pair, none on a dry run."""
    if dry_run:
        return []
    return [_queue_one(conflict, umbrella_url=umbrella_url) for conflict in conflicts]


def _queue_one(conflict: "BindingConflict", *, umbrella_url: str) -> TicketOutcome:
    key = _conflict_key(conflict)
    title = f"Conflicting BINDING memories need reconciliation: {neutralize_bare_references(key)}"
    detail = (
        "Two BINDING memory files are near-duplicates but cannot be auto-merged — binding doctrine that "
        "disagrees must be reconciled by a human, not silently collapsed. Decide which rule is canonical and "
        f"retire or rewrite the other. Files: `{conflict.survivor_name}.md`, `{conflict.absorbed_name}.md`."
    )
    reason = _withholding_reason(f"{title}\n{detail}")
    if reason:
        return TicketOutcome(cluster_key=key, filed=False, withheld=True, reason=reason)
    owner = covering_ticket(key)
    if owner is not None:
        return TicketOutcome(cluster_key=key, filed=False, ticket_url=owner.issue_url, reason="already queued or owned")
    umbrella = umbrella_ticket(umbrella_url)
    if umbrella is None:
        return TicketOutcome(cluster_key=key, filed=False, reason=f"no ticket tracks the dream umbrella {umbrella_url}")
    queue_pending(
        umbrella,
        [{"gap_key": key, "cluster_key": key, "title": title, "citation": "no-memory-row", "detail": detail}],
    )
    return TicketOutcome(cluster_key=key, filed=True, ticket_url=umbrella_url, reason="queued for the backlog sweep")
