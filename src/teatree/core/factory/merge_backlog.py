"""The standing merge backlog: which CLEARs are unconsumed, and which were moved past.

One concern, one module — the population every merge-scoped reader shares. The S4
staleness trip and ``t3 doctor check``'s backlog report both read
:func:`unconsumed_actionable_clears`, because two surfaces answering the same question
from two queries is exactly how one of them came to report healthy while 87 merge
authorisations stood unconsumed (#4250).
"""

from dataclasses import dataclass
from datetime import datetime

from teatree.core.merge.clear_scope import clear_scope_predicate
from teatree.core.models.merge_clear import MergeAudit, MergeClear

# A stale actionable CLEAR older than this is a stalled merge loop, not a slow one.
STALE_CLEAR_HOURS = 48.0


def superseding_context(overlay: str) -> tuple[dict[tuple[str, int], datetime], set[tuple[str, int]]]:
    """The two supersede signals S4's staleness trip consults, each one grouped read (#15).

    ``(latest_issued, merged_keys)`` keyed on the raw ``MergeClear.slug`` (a
    re-CLEAR of the same workstream PR shares its older sibling's ``(slug,
    pr_id)``): the newest ``issued_at`` across ALL CLEARs for a key, and every
    ``(slug, pr_id)`` that already has a ``MergeAudit`` (the PR merged). Together
    they identify an unconsumed CLEAR the merge loop has moved past — a
    strictly-newer sibling re-reviewed it forward, or a merge already covers it.

    Public because the waiting-lane covering-CLEAR match (:func:`~teatree.core.waiting._has_covering_clear`,
    #21) reads the SAME context and applies the SAME :func:`clear_is_superseded`
    predicate — a superseded orphan must not authorise a merge there while S4
    excludes it here, or the two lanes diverge on the SIG-1 supersede semantics.
    An empty ``overlay`` scopes globally, which is what the per-PR waiting match
    wants so a ticket-less CLEAR's siblings are seen regardless of overlay.

    Scoped through the SAME :func:`clear_scope_predicate` the age signal reads
    (#4250), and that lockstep is load-bearing: widening the age population while
    this context stayed on the ticket join would leave every ticket-less row
    reading as non-superseded, so a moved-past CLEAR would alarm forever.
    """
    in_scope = clear_scope_predicate(overlay)
    # Both scans are deliberately whole-ledger — a re-CLEAR shares its sibling's
    # ``(slug, pr_id)`` across time, so the newest issue and every covering merge
    # for a key must be seen regardless of window. ``.iterator()`` streams each so
    # the unbounded ledgers cap peak memory rather than materialising in full.
    latest_issued: dict[tuple[str, int], datetime] = {}
    for clear in MergeClear.objects.select_related("ticket").iterator():
        if not in_scope(clear):
            continue
        key = (clear.slug, clear.pr_id)
        if key not in latest_issued or clear.issued_at > latest_issued[key]:
            latest_issued[key] = clear.issued_at
    merged_keys = {
        (audit.clear.slug, audit.clear.pr_id)
        for audit in MergeAudit.objects.select_related("clear", "clear__ticket").iterator()
        if in_scope(audit.clear)
    }
    return latest_issued, merged_keys


def clear_is_superseded(
    clear: MergeClear,
    latest_issued: dict[tuple[str, int], datetime],
    merged_keys: set[tuple[str, int]],
) -> bool:
    """True iff *clear* has been moved past — the shared SIG-1 supersede predicate (#15/#21).

    A CLEAR is superseded when a ``MergeAudit`` already covers its ``(slug,
    pr_id)`` (the PR merged) or a strictly-newer sibling CLEAR exists for the
    same key (a head-move re-review issued forward). The single predicate S4's
    staleness trip and the waiting-lane covering match both apply against a
    :func:`superseding_context`, so an orphaned old CLEAR is treated identically
    on both lanes instead of one counting it live and the other excluding it.
    """
    key = (clear.slug, clear.pr_id)
    if key in merged_keys:
        return True
    return latest_issued.get(key, clear.issued_at) > clear.issued_at


@dataclass(frozen=True, slots=True)
class UnconsumedClear:
    """One standing merge authorisation nothing has executed — the backlog row (#4250)."""

    slug: str
    pr_id: int
    reviewed_sha: str
    issued_at: datetime
    age_hours: float

    @property
    def ref(self) -> str:
        return f"{self.slug}#{self.pr_id}"

    def describe(self) -> str:
        stamp = self.issued_at.isoformat()
        return f"{self.ref} cleared at {stamp} ({self.age_hours:.1f}h ago, sha {self.reviewed_sha[:8]})"


def unconsumed_actionable_clears(overlay: str, now: datetime) -> list[UnconsumedClear]:
    """The standing merge backlog, oldest first — the one query every surface reads.

    A CLEAR the merge loop has moved past is NOT a stalled merge and is excluded
    (#15): a strictly-newer sibling CLEAR exists for the same ``(slug, pr_id)``, or
    a ``MergeAudit`` already covers that PR (the orphaned-row backstop to the
    merge-time sibling supersede in ``record_merge_and_advance``, catching a legacy
    or cross-tick sibling the supersede never reached). Without this, one head-move
    re-review left the older CLEAR unconsumed forever and ratcheted S4 hard-red
    permanently after 48h. A genuinely-stale CLEAR — no newer sibling, no covering
    merge — is returned.

    Public because the S4 staleness trip and ``t3 doctor check``'s backlog report
    both consume it: two surfaces answering the same question from two queries is
    how one of them came to report healthy while 87 rows stood unconsumed.
    """
    in_scope = clear_scope_predicate(overlay)
    qs = MergeClear.objects.filter(consumed_at__isnull=True).select_related("ticket")
    actionable = [clear for clear in qs if in_scope(clear) and clear.is_actionable()]
    if not actionable:
        return []
    latest_issued, merged_keys = superseding_context(overlay)
    rows = [
        UnconsumedClear(
            slug=clear.slug,
            pr_id=clear.pr_id,
            reviewed_sha=clear.reviewed_sha,
            issued_at=clear.issued_at,
            age_hours=(now - clear.issued_at).total_seconds() / 3600.0,
        )
        for clear in actionable
        if not clear_is_superseded(clear, latest_issued, merged_keys)
    ]
    return sorted(rows, key=lambda row: row.issued_at)


def max_actionable_clear_age_hours(overlay: str, now: datetime) -> float | None:
    """Age in hours of the oldest standing merge authorisation, or ``None``."""
    backlog = unconsumed_actionable_clears(overlay, now)
    return backlog[0].age_hours if backlog else None
