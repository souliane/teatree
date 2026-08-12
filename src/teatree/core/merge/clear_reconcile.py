"""Spend the merge authorisations whose PR already settled — convergence to zero (#4250).

Reclassifying a merged PR out of the alarm stops the false page, but on its own it
only moves a permanent finding one severity down: the row stands unconsumed
forever and every surface keeps reporting it. This pass is what empties the
population — a CLEAR whose PR is MERGED or CLOSED on the forge has nothing left to
authorise, so ``consumed_at`` is stamped and the row leaves the backlog.

No ``MergeAudit`` is written. A ``MergeAudit`` means "the keystone executed this
merge", and back-filling one for a merge that happened outside the keystone would
corrupt the very signal S1-S4 read.
:meth:`~teatree.core.models.pull_request.PullRequest.record_forge_merge` is the
precedent for recording an out-of-band landing without inventing keystone
provenance, and ``clear_backfill`` already models a consumed CLEAR with no audit.

Fail-closed: only a definite MERGED/CLOSED settles anything — UNVERIFIED settles
nothing, so an unreadable forge leaves the ledger exactly as it found it.
"""

from dataclasses import dataclass, field
from datetime import datetime

from teatree.core.factory.merge_backlog import unconsumed_actionable_clear_rows
from teatree.core.merge.clear_liveness import ClearLiveness, PrStateReader, classify, unverified_reader
from teatree.core.models.merge_clear import MergeClear


@dataclass(frozen=True, slots=True)
class ClearReconcileReport:
    """What one reconcile pass settled, and what it deliberately left alone."""

    settled: list[str] = field(default_factory=list)
    stalled: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    dry_run: bool = False

    def lines(self) -> list[str]:
        verb = "would settle" if self.dry_run else "settled"
        rows = [f"{verb} {ref}" for ref in self.settled]
        rows += [f"stalled (PR still open) {ref}" for ref in self.stalled]
        rows += [f"unverified (no forge evidence) {ref}" for ref in self.unverified]
        return rows or ["no unconsumed merge authorisation to reconcile"]


def reconcile_settled_clears(
    *,
    read_state: PrStateReader = unverified_reader,
    now: datetime,
    overlay: str = "",
    dry_run: bool = False,
) -> ClearReconcileReport:
    """Stamp ``consumed_at`` on every standing CLEAR whose PR already merged or closed.

    Idempotent and self-limiting: a consumed row leaves
    :func:`~teatree.core.factory.merge_backlog.unconsumed_actionable_clear_rows`,
    so each run shrinks the set the next one probes. Per-row isolated — one
    unreadable PR is UNVERIFIED for itself alone.
    """
    report = ClearReconcileReport(dry_run=dry_run)
    for clear in unconsumed_actionable_clear_rows(overlay):
        ref = f"{clear.slug}#{clear.pr_id}"
        liveness = classify(clear, read=read_state)
        if liveness is ClearLiveness.STALLED:
            report.stalled.append(ref)
        elif liveness is ClearLiveness.UNVERIFIED:
            report.unverified.append(ref)
        else:
            report.settled.append(f"{ref} ({liveness})")
            if not dry_run:
                MergeClear.objects.filter(pk=clear.pk).update(consumed_at=now)
    return report
