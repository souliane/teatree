"""The standing merge backlog: which CLEARs are unconsumed, and which were moved past.

One concern, one module — the population every merge-scoped reader shares. The S4
staleness trip and ``t3 doctor check``'s backlog report both read
:func:`unconsumed_actionable_clears`, because two surfaces answering the same question
from two queries is exactly how one of them came to report healthy while 87 merge
authorisations stood unconsumed (#4250).
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from teatree.core.merge.clear_scope import clear_scope_predicate
from teatree.core.models.merge_clear import MergeAudit, MergeClear

# A stale actionable CLEAR older than this is a stalled merge loop, not a slow one.
STALE_CLEAR_HOURS = 48.0


def _merged_diff_key(clear: MergeClear) -> tuple[int, str] | None:
    """*clear*'s ``(pr_id, reviewed_sha)`` identity, or ``None`` when it carries no sha."""
    sha = clear.reviewed_sha.strip().lower()
    return (clear.pr_id, sha) if sha else None


@dataclass(frozen=True, slots=True)
class SupersedeContext:
    """The three signals that identify a CLEAR the merge loop has already moved past (#15/#21).

    ``latest_issued`` and ``merged_keys`` are keyed on the raw ``MergeClear.slug``
    (a re-CLEAR of the same workstream PR shares its older sibling's ``(slug,
    pr_id)``): the newest ``issued_at`` across ALL CLEARs for a key, and every
    ``(slug, pr_id)`` that already has a ``MergeAudit``.

    ``merged_shas`` is the slug-independent third signal (#4250): every
    ``(pr_id, reviewed_sha)`` a ``MergeAudit`` covers. A 40-char ``reviewed_sha``
    identifies the exact reviewed tree, so a sibling audit on the same key is
    unambiguous proof that this exact diff merged however either row spelled its
    slug — which is what the slug-keyed signals miss when one sibling stored a
    head branch there instead of an ``owner/repo``.
    """

    latest_issued: dict[tuple[str, int], datetime]
    merged_keys: set[tuple[str, int]]
    merged_shas: set[tuple[int, str]]


def superseding_context(overlay: str) -> SupersedeContext:
    """The supersede signals S4's staleness trip consults, each one grouped read (#15).

    Together they identify an unconsumed CLEAR the merge loop has moved past — a
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
    merged_keys: set[tuple[str, int]] = set()
    merged_shas: set[tuple[int, str]] = set()
    for audit in MergeAudit.objects.select_related("clear", "clear__ticket").iterator():
        if not in_scope(audit.clear):
            continue
        merged_keys.add((audit.clear.slug, audit.clear.pr_id))
        diff_key = _merged_diff_key(audit.clear)
        if diff_key is not None:
            merged_shas.add(diff_key)
    return SupersedeContext(latest_issued=latest_issued, merged_keys=merged_keys, merged_shas=merged_shas)


def clear_is_superseded(clear: MergeClear, context: SupersedeContext) -> bool:
    """True iff *clear* has been moved past — the shared SIG-1 supersede predicate (#15/#21).

    A CLEAR is superseded when a ``MergeAudit`` already covers its ``(slug,
    pr_id)`` or its ``(pr_id, reviewed_sha)`` (the PR merged), or a strictly-newer
    sibling CLEAR exists for the same slug key (a head-move re-review issued
    forward). The single predicate S4's staleness trip and the waiting-lane
    covering match both apply against a :func:`superseding_context`, so an
    orphaned old CLEAR is treated identically on both lanes instead of one
    counting it live and the other excluding it.

    Widening to the sha key narrows what the waiting lane may merge — a
    superseded orphan stops authorising a merge — which is the conservative
    direction for that consumer.
    """
    key = (clear.slug, clear.pr_id)
    if key in context.merged_keys:
        return True
    diff_key = _merged_diff_key(clear)
    if diff_key is not None and diff_key in context.merged_shas:
        return True
    return context.latest_issued.get(key, clear.issued_at) > clear.issued_at


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

    @classmethod
    def of(cls, clear: MergeClear, now: datetime) -> "UnconsumedClear":
        return cls(
            slug=clear.slug,
            pr_id=clear.pr_id,
            reviewed_sha=clear.reviewed_sha,
            issued_at=clear.issued_at,
            age_hours=(now - clear.issued_at).total_seconds() / 3600.0,
        )


def unconsumed_actionable_clear_rows(overlay: str) -> list[MergeClear]:
    """The standing merge backlog as its own ``MergeClear`` rows, oldest first (#4250).

    The row-level form of :func:`unconsumed_actionable_clears`, for the consumers
    that need more of the row than the report DTO carries — the liveness classifier
    wants ``host_kind`` and the ticket link to build a PR URL, and the reconciler
    stamps ``consumed_at`` on the row itself. Both read this population rather than
    re-deriving one, which is the whole point of the module. The population is not
    time-windowed, so it takes no clock.
    """
    in_scope = clear_scope_predicate(overlay)
    qs = MergeClear.objects.filter(consumed_at__isnull=True).select_related("ticket")
    actionable = [clear for clear in qs if in_scope(clear) and clear.is_actionable()]
    if not actionable:
        return []
    context = superseding_context(overlay)
    live = [clear for clear in actionable if not clear_is_superseded(clear, context)]
    return sorted(live, key=lambda clear: clear.issued_at)


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
    return [UnconsumedClear.of(clear, now) for clear in unconsumed_actionable_clear_rows(overlay)]


def max_actionable_clear_age_hours(overlay: str, now: datetime) -> float | None:
    """Age in hours of the oldest standing merge authorisation, or ``None``."""
    backlog = unconsumed_actionable_clears(overlay, now)
    return backlog[0].age_hours if backlog else None


class ClearStanding(StrEnum):
    """Why an unconsumed CLEAR is, or is not, part of the standing merge backlog.

    ``LIVE`` is the population :func:`unconsumed_actionable_clear_rows` returns and
    every existing surface reports. The other two are the rows those surfaces
    deliberately filter out — correctly, because neither can authorise a merge — and
    which therefore appear nowhere at all.
    """

    LIVE = "live"
    SUPERSEDED = "superseded"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class OutstandingClear:
    """One unconsumed CLEAR and the standing that decides which surfaces can see it."""

    pk: int
    slug: str
    pr_id: int
    reviewed_sha: str
    reviewer_identity: str
    issued_at: datetime
    standing: ClearStanding

    @property
    def ref(self) -> str:
        return f"{self.slug}#{self.pr_id}"

    def describe(self) -> str:
        stamp = self.issued_at.isoformat()
        sha = self.reviewed_sha[:8] or "(none)"
        return (
            f"{self.standing} {self.ref} clear={self.pk} sha={sha} by {self.reviewer_identity or '(none)'} at {stamp}"
        )


def _standing_of(clear: MergeClear, context: SupersedeContext) -> ClearStanding:
    """*clear*'s standing, checked in the order that names the FIRST reason it is unmergeable."""
    if not clear.is_actionable():
        return ClearStanding.INCOMPLETE
    if clear_is_superseded(clear, context):
        return ClearStanding.SUPERSEDED
    return ClearStanding.LIVE


def outstanding_clear_rows(overlay: str) -> list[OutstandingClear]:
    """EVERY unconsumed CLEAR, classified — including the ones no other surface reports.

    :func:`unconsumed_actionable_clear_rows` narrows to what can still authorise a
    merge, so a row that is incomplete (a mis-issue missing a load-bearing field) or
    superseded (a newer sibling or a covering merge moved past it) is filtered out of
    the backlog, the S4 staleness trip, ``t3 doctor check``, and — because it reads
    the same narrowed population — ``ticket reconcile-clears``. Nothing then lists
    them, so a mis-issued authorisation stands in the durable governance store
    forever with no supported way to even see it.

    This is the deliberately unnarrowed read: the same scope predicate and the same
    supersede context, but every unconsumed row returned with its standing named
    rather than dropped. Oldest first, so a ledger read top-down is chronological.
    """
    in_scope = clear_scope_predicate(overlay)
    qs = MergeClear.objects.filter(consumed_at__isnull=True).select_related("ticket")
    unconsumed = [clear for clear in qs if in_scope(clear)]
    if not unconsumed:
        return []
    context = superseding_context(overlay)
    rows = [
        OutstandingClear(
            pk=clear.pk,
            slug=clear.slug,
            pr_id=clear.pr_id,
            reviewed_sha=clear.reviewed_sha,
            reviewer_identity=clear.reviewer_identity,
            issued_at=clear.issued_at,
            standing=_standing_of(clear, context),
        )
        for clear in unconsumed
    ]
    return sorted(rows, key=lambda row: row.issued_at)
