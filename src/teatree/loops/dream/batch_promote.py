"""One dream pass's promotions queue for the backlog sweep; the pass mints no ticket (#4776).

:class:`PromotionBatch` is built once per pass. Its :meth:`PromotionBatch.consider`
method grounds, deduplicates, and withholds gaps before collecting them. After
promotion runs, :func:`promote_batch` appends the collected gaps to the
umbrella host ticket's pending ledger (:mod:`teatree.core.models.dream_gap_ledger`) and
mints nothing: the backlog sweep folds each gap into an EXISTING host through
``t3 <overlay> ticket attach-gaps``, exactly as it groups issues. Batch tickets
drain through :func:`reconcile_batches`.

**The failure mode this is designed against** (the same one #4410 names): a batched PR
that claims 9 of 10 gaps fixed but only delivers fewer is WORSE than a PR that
delivers 1 honestly, because the unfixed gap's checkbox would get ticked and vanish
from the ledger. So a batch ticket's checkbox is ticked, and its gap's memory
retired, ONLY for the subset the coder recorded as delivered
(``extra['dream_gap_claimed_delivered']``) — verified independently at review — never
for the whole manifest on the strength of the PR merging. An undelivered gap stays
unchecked and :meth:`PromotionBatch.consider` finds it uncovered again on the next
pass, so nothing promoted is ever silently lost.
"""

import hashlib
import logging
from dataclasses import dataclass, field

from django.db import transaction

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConsolidatedMemory
from teatree.core.models.dream_gap_ledger import (
    BATCH_KEY,
    CLAIMED_DELIVERED_KEY,
    DISPOSITIONS_KEY,
    REJECT,
    pending_entries,
    queue_pending,
    umbrella_ticket,
)
from teatree.core.models.ticket import Ticket
from teatree.core.models.types import DreamGapEntry
from teatree.core.review.review_findings import neutralize_bare_references
from teatree.loops.dream.umbrella_ledger import (
    _BATCH_MARKER_PREFIX,
    _UMBRELLA_KEY,
    GapSpec,
    _ensure_gap_checked,
    _merge_evidence_url,
    _stamp_memory_merged,
    _stamp_memory_promoted,
    _stamp_ticket_reconciled,
    _withholding_reason,
    code_host_for,
    is_promotion_anchor,
)

logger = logging.getLogger(__name__)

_RECONCILED_KEY = "dream_gap_reconciled_at"


@dataclass(frozen=True, slots=True)
class ConsiderOutcome:
    """The result of considering one gap for this pass's batch.

    ``queued`` is True only when this gap was newly appended to the batch's pending
    set; ``withheld`` mirrors ``promote_gap``'s banned-term/bare-reference gate;
    ``already_covered`` is True when an in-flight or already-delivered ticket already
    carries this gap key, so queuing it again would duplicate work in flight.
    """

    gap_key: str
    queued: bool
    withheld: bool = False
    already_covered: bool = False
    reason: str = ""


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """The result of queueing (or not queueing) this pass's gaps on the umbrella host."""

    queued: bool
    umbrella_url: str
    gap_count: int
    reason: str = ""


@dataclass(slots=True)
class PromotionBatch:
    """One pass's promotable gaps, collected across all three promoting phases.

    Replaces ``PromotionBudget`` (#4776): with no per-gap ticket there is nothing left
    to ration, so gaps are simply COLLECTED as each phase discovers them via
    :meth:`consider`, and :func:`promote_batch` queues them once every phase has run.
    """

    pending: list[GapSpec] = field(default_factory=list)
    withheld: int = 0
    already_covered: int = 0

    def consider(self, *, gap: GapSpec, dry_run: bool = False) -> ConsiderOutcome:
        """Ground/withhold/dedup one gap and queue it for this pass's batch ticket.

        Mirrors ``promote_gap``'s banned-term/bare-reference withholding, but never
        itself writes anything — that happens ONCE, in :func:`promote_batch`, after
        every phase has considered its gaps.

        Coverage (:func:`gap_covered`) is decided from TICKET state alone, never from
        whether the umbrella body happens to carry this gap's marker: a checkbox line
        can outlive the ticket that added it (a reconciled batch that DROPPED this
        gap leaves its unchecked line sitting there), and re-offering that gap must
        not read a stale line as "still in flight" forever. Re-adding an
        already-present line is harmless — :func:`promote_batch` dedupes by marker.
        """
        safe_title = neutralize_bare_references(gap.title.strip())
        reason = _withholding_reason(safe_title) or (gap.detail and _withholding_reason(gap.detail))
        if reason:
            self.withheld += 1
            # The banned-terms ruleset is versioned: a title promoted last night can be
            # withheld tonight while its checkbox/ticket from that promotion stays live —
            # so coverage is still reported here, not dropped just because this pass
            # cannot (re)write the title.
            return ConsiderOutcome(
                gap_key=gap.gap_key,
                queued=False,
                withheld=True,
                already_covered=gap_covered(gap.gap_key),
                reason=reason,
            )
        if dry_run:
            return ConsiderOutcome(gap_key=gap.gap_key, queued=False, reason="DRY (no writes)")
        if any(queued.gap_key == gap.gap_key for queued in self.pending):
            return ConsiderOutcome(gap_key=gap.gap_key, queued=False, reason="already queued this pass")
        if gap_covered(gap.gap_key):
            self.already_covered += 1
            return ConsiderOutcome(
                gap_key=gap.gap_key, queued=False, already_covered=True, reason="already pending or owned by a host"
            )
        self.pending.append(
            GapSpec(gap_key=gap.gap_key, title=safe_title, cluster_key=gap.cluster_key, detail=gap.detail)
        )
        return ConsiderOutcome(gap_key=gap.gap_key, queued=True, reason="queued for this pass's batch")

    @property
    def summary(self) -> str:
        """The pass-summary clause naming what this pass's collection turned away."""
        if not self.withheld and not self.already_covered:
            return ""
        clauses = []
        if self.withheld:
            clauses.append(f"{self.withheld} withheld")
        if self.already_covered:
            clauses.append(f"{self.already_covered} already covered")
        return f"; batch collected {len(self.pending)} gap(s), " + ", ".join(clauses)


def _batch_tickets() -> "list[Ticket]":
    return list(Ticket.objects.exclude(extra__dream_gap_batch__isnull=True).exclude(state=Ticket.State.IGNORED))


def gap_covered(gap_key: str) -> bool:
    return covering_ticket(gap_key) is not None


def covering_ticket(gap_key: str) -> Ticket | None:
    """The ticket *gap_key* already rides — pending on the umbrella or owned by a host.

    Pending on the umbrella host's ledger covers it until the sweep folds it into a host.
    A host (a sweep fold target, or a pre-sweep batch ticket) covers it while unreconciled,
    and once reconciled only if it DELIVERED the gap; a reconciled ticket that dropped it
    reports it uncovered, so :meth:`PromotionBatch.consider` re-offers it. Every matching
    ticket is checked, never just the first — a stale dropped ticket must not hide a
    fresh in-flight one.
    """
    for ticket in Ticket.objects.exclude(extra__dream_gap_pending__isnull=True):
        if any(entry.get("gap_key") == gap_key for entry in pending_entries(ticket)):
            return ticket
    for ticket in _batch_tickets():
        extra = ticket.extra or {}
        keys = {entry.get("gap_key") for entry in extra.get(BATCH_KEY) or [] if isinstance(entry, dict)}
        if gap_key not in keys:
            continue
        if not is_reconciled(ticket):
            return ticket
        if gap_key in settled_gap_keys(ticket):
            return ticket
    return None


def is_reconciled(ticket: Ticket) -> bool:
    return bool((ticket.extra or {}).get(_RECONCILED_KEY))


def _batch_anchor(umbrella_url: str, gap_keys: "list[str]") -> str:
    """The promotion anchor a queued gap's memory row is stamped with, keyed on the pass's gap set."""
    digest = hashlib.sha256("\n".join(sorted(gap_keys)).encode()).hexdigest()[:16]
    return f"{umbrella_url}#{_BATCH_MARKER_PREFIX}={digest}"


def _citation_status(cluster_key: str) -> str:
    row = ConsolidatedMemory.objects.filter(cluster_key=cluster_key).first()
    if row is None:
        return "no-memory-row"
    return "cited" if row.verified_citation.strip() else "uncited"


def _pending_entry(gap: GapSpec) -> DreamGapEntry:
    entry: DreamGapEntry = {
        "gap_key": gap.gap_key,
        "cluster_key": gap.cluster_key,
        "title": gap.title.strip(),
        "citation": _citation_status(gap.cluster_key),
    }
    if gap.detail:
        entry["detail"] = gap.detail
    return entry


def promote_batch(*, umbrella_url: str, batch: PromotionBatch, dry_run: bool = False) -> BatchOutcome:
    """Queue everything this pass collected on the umbrella host's pending ledger; mint nothing.

    An empty batch queues nothing — the negative control. With no ticket tracking
    *umbrella_url* nothing is queued and every gap stays in the drain queue, re-offered
    next pass. Each queued gap's memory is stamped TICKETED on a promotion anchor in the
    same transaction, so it leaves ``needs_ticket()`` exactly when the ledger holds it.
    """
    if not batch.pending:
        return BatchOutcome(queued=False, umbrella_url=umbrella_url, gap_count=0, reason="nothing pending")
    if dry_run:
        return BatchOutcome(
            queued=False, umbrella_url=umbrella_url, gap_count=len(batch.pending), reason="DRY (no writes)"
        )
    host = umbrella_ticket(umbrella_url)
    if host is None:
        return BatchOutcome(
            queued=False,
            umbrella_url=umbrella_url,
            gap_count=len(batch.pending),
            reason=f"no ticket tracks the dream umbrella {umbrella_url} — gaps stay in the drain queue",
        )
    anchor = _batch_anchor(umbrella_url, [gap.gap_key for gap in batch.pending])
    with transaction.atomic():
        queue_pending(host, [_pending_entry(gap) for gap in batch.pending])
        for gap in batch.pending:
            _stamp_memory_promoted(gap.cluster_key, anchor_url=anchor)
    return BatchOutcome(
        queued=True, umbrella_url=umbrella_url, gap_count=len(batch.pending), reason="queued for the backlog sweep"
    )


def _pending_reconcile_batch_tickets() -> "list[Ticket]":
    return list(
        Ticket.objects.exclude(extra__dream_gap_batch__isnull=True).filter(extra__dream_gap_reconciled_at__isnull=True)
    )


def settled_gap_keys(ticket: Ticket) -> set[str]:
    """The gap keys a reconciled *ticket* still owns: delivered, or rejected with a reason."""
    extra = ticket.extra or {}
    rejected = {
        key
        for key, record in (extra.get(DISPOSITIONS_KEY) or {}).items()
        if isinstance(record, dict) and record.get("disposition") == REJECT
    }
    return set(extra.get(CLAIMED_DELIVERED_KEY) or []) | rejected


def _reopen_dropped_gap(cluster_key: str, *, gap_key: str) -> None:
    """Return a dropped gap's memory to the drain unless a live owner in the ledger still holds it.

    Keyed on the gap ledger, never on the row's url: a row carries whichever promotion
    anchor last stamped it (the umbrella's, for a gap the sweep folded into a host).
    """
    row = ConsolidatedMemory.objects.filter(
        cluster_key=cluster_key, disposition=ConsolidatedMemory.Disposition.TICKETED
    ).first()
    if row is None or not is_promotion_anchor(row.ticket_url) or gap_covered(gap_key):
        return
    row.reopen_core_gap()


def reconcile_batches(host: CodeHostBackend, *, umbrella_url: str) -> "list[Ticket]":
    """Check + retire only the DELIVERED gaps of every MERGED batch ticket or sweep host (#4776).

    A sweep host (no ``dream_umbrella_url`` of its own) carries no umbrella checkbox, so its
    delivered gaps need none confirmed; a pre-sweep batch ticket confirms its box on the
    umbrella IT recorded, through that umbrella's own forge.

    Reads ``dream_gap_claimed_delivered`` (the coder/reviewer-recorded subset),
    intersected with the ticket's own manifest so an out-of-manifest key is never
    trusted. Each delivered gap's checkbox is checked and its memory stamped via the
    existing umbrella-ledger helpers, verbatim. The ticket is stamped reconciled only
    once EVERY delivered gap's checkbox was confirmed checked — mirroring
    the batch's all-or-nothing-per-attempt semantics — so a partial
    forge failure retries the whole ticket next pass rather than leaving a gap's box
    permanently unconfirmed. A gap the coder dropped keeps its unchecked checkbox, and
    its memory returns to ``needs_ticket()`` while it still points at THIS ticket, so
    the next pass re-offers it; a row a newer batch re-stamped is left alone.

    Retirement is keyed on each confirmed gap's cluster key, never on a url alone: with
    no merged PR row the evidence url is the ticket's own anchor, which every row
    stamped on the batch shares, dropped gaps included. On that anchor a partially
    confirmed ticket retires nothing until the whole ticket reconciles.
    """
    from teatree.loops.dream.promote_memory import retire_resolved_memories  # noqa: PLC0415 — tick-time import

    reconciled: list[Ticket] = []
    retirable: set[tuple[str, str]] = set()
    for ticket in _pending_reconcile_batch_tickets():
        if ticket.state != Ticket.State.MERGED:
            continue
        extra = ticket.extra or {}
        manifest = {
            entry["gap_key"]: entry
            for entry in extra.get(BATCH_KEY) or []
            if isinstance(entry, dict) and entry.get("gap_key")
        }
        delivered_keys = set(extra.get(CLAIMED_DELIVERED_KEY) or []) & manifest.keys()
        own_umbrella = str(extra.get(_UMBRELLA_KEY) or "")
        forge = host if own_umbrella in {"", umbrella_url} else code_host_for(own_umbrella)
        merged_url = _merge_evidence_url(ticket)
        all_confirmed = True
        confirmed: set[tuple[str, str]] = set()
        for gap_key in delivered_keys:
            if own_umbrella and (
                forge is None or not _ensure_gap_checked(forge, umbrella_url=own_umbrella, gap_key=gap_key).is_checked
            ):
                all_confirmed = False
                logger.warning(
                    "dream batch reconcile: could not check umbrella box for gap %r — retrying next pass", gap_key
                )
                continue
            cluster_key = str(manifest[gap_key].get("cluster_key") or gap_key)
            _stamp_memory_merged(cluster_key, merged_url=merged_url)
            confirmed.add((cluster_key, merged_url))
        if all_confirmed or merged_url != ticket.issue_url:
            retirable |= confirmed
        if not all_confirmed:
            continue
        _stamp_ticket_reconciled(ticket)
        for gap_key in manifest.keys() - settled_gap_keys(ticket):
            _reopen_dropped_gap(str(manifest[gap_key].get("cluster_key") or gap_key), gap_key=gap_key)
        reconciled.append(ticket)
    if retirable:
        retire_resolved_memories(host, is_resolved=lambda row: (row.cluster_key, row.ticket_url) in retirable)
    return reconciled


__all__ = [
    "BatchOutcome",
    "ConsiderOutcome",
    "PromotionBatch",
    "covering_ticket",
    "gap_covered",
    "is_reconciled",
    "promote_batch",
    "reconcile_batches",
]
