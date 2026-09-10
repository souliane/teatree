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

DISPOSAL is the third outcome (#4739). A CLEAR whose PR the forge says never existed
can never acquire the evidence this pass waits for, so the fail-closed refusal held it
``live`` forever and the ledger's standing set answered "what may merge?" wrongly. Such
a row is spent with its reason recorded in a ``MergeClearDisposal``, and the whole
``(slug, pr_id)`` family goes with it — the 404 is evidence about the PR number, not
about one row.
"""

from dataclasses import dataclass, field
from datetime import datetime

from teatree.core.factory.merge_backlog import unconsumed_actionable_clear_rows
from teatree.core.merge.clear_liveness import ClearLiveness, PrStateReader, classify, clear_pr_url, unverified_reader
from teatree.core.models.merge_clear import MergeClear
from teatree.core.models.merge_clear_disposal import MergeClearDisposal


@dataclass(frozen=True, slots=True)
class ClearReconcileReport:
    """What one reconcile pass settled, and what it deliberately left alone."""

    settled: list[str] = field(default_factory=list)
    disposed: list[str] = field(default_factory=list)
    stalled: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    dry_run: bool = False

    def lines(self) -> list[str]:
        verb = "would settle" if self.dry_run else "settled"
        disposal = "would dispose" if self.dry_run else "disposed"
        rows = [f"{verb} {ref}" for ref in self.settled]
        rows += [f"{disposal} (PR absent on forge) {ref}" for ref in self.disposed]
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
    spent: set[int] = set()
    for clear in unconsumed_actionable_clear_rows(overlay):
        # A disposal consumes the whole family, so a sibling this pass already spent is
        # neither re-probed nor re-reported — the population was read before any write.
        if clear.pk in spent:
            continue
        ref = f"{clear.slug}#{clear.pr_id}"
        liveness = classify(clear, read=read_state)
        if liveness is ClearLiveness.STALLED:
            report.stalled.append(ref)
        elif liveness is ClearLiveness.UNVERIFIED:
            report.unverified.append(ref)
        elif liveness is ClearLiveness.PHANTOM:
            family = _phantom_family(clear, now=now, dry_run=dry_run)
            spent.update(family)
            report.disposed.append(f"{ref} ({len(family)} row(s))")
        else:
            report.settled.append(f"{ref} ({liveness})")
            if not dry_run:
                MergeClear.objects.filter(pk=clear.pk).update(consumed_at=now)
    return report


def _phantom_family(clear: MergeClear, *, now: datetime, dry_run: bool) -> set[int]:
    """The pks a phantom disposal covers, spending them unless this is a dry run."""
    if dry_run:
        return set(
            MergeClear.objects.filter(
                slug=clear.slug,
                pr_id=clear.pr_id,
                consumed_at__isnull=True,
            ).values_list("pk", flat=True)
        )
    disposed = MergeClearDisposal.objects.dispose_phantom_family(clear, pr_url=clear_pr_url(clear), now=now)
    return {row.pk for row in disposed}
