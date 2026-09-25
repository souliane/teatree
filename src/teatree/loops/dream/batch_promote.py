"""One dream pass's promotions become ONE ticket and ONE PR, never one per gap (#4776).

The three promoting phases — core-gap memory promotion (Pass 2), compliance
escalation (3c), the automatable-ask promoter (3d) — used to drive every gap through
``umbrella_ledger``'s old ``promote_gap`` chokepoint (now removed), which scheduled a
fresh coding task PER GAP: one gap, one ticket, one PR, each paying a full
plan → implement → review → CI cycle. ``promotion_cap`` rationed that fan-out; it did
not make it cheaper. A measured pass with 297 pending gaps and a cap of 5 defers
roughly 60 nights to drain, against a queue that runs a handful of tasks concurrently
— a backlog that is not draining, it is permanently deferred.

This module replaces the ration with the actual fix: the batch boundary is the PASS
itself, not a per-gap or per-cluster unit. :class:`PromotionBatch` is built ONCE per
pass and threaded through all three phases; each phase calls :meth:`PromotionBatch.consider`
per gap it discovers (grounding, dedup-by-coverage, and banned-term/bare-reference
withholding — the same checks ``promote_gap`` used to run), which only COLLECTS the
gap. After every phase has run, :func:`promote_batch` mints AT MOST ONE ticket
carrying every pending gap's manifest, once, via the existing
:meth:`~teatree.core.models.ticket.Ticket.schedule_coding` keystone.

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

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.core.review.review_findings import neutralize_bare_references
from teatree.loops.dream.umbrella_ledger import (
    _UMBRELLA_KEY,
    GapSpec,
    _ensure_gap_checked,
    _line_index,
    _merged_pr_url,
    _read_body,
    _scrubbed_update,
    _stamp_memory_merged,
    _stamp_ticket_reconciled,
    _withholding_reason,
    render_checkbox_line,
)

logger = logging.getLogger(__name__)

#: ``ticket.extra`` keys for a BATCH ticket — distinct from ``umbrella_ledger``'s
#: single-gap ``dream_gap_key``/``dream_memory_cluster_key`` so a legacy in-flight
#: per-gap ticket and a new batch ticket are never confused by either scan.
_BATCH_KEY = "dream_gap_batch"
_CLAIMED_DELIVERED_KEY = "dream_gap_claimed_delivered"
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
    """The result of minting (or not minting) this pass's single batch ticket."""

    scheduled: bool
    ticket_url: str
    gap_count: int
    checkboxes_added: int = 0
    reason: str = ""


@dataclass(slots=True)
class PromotionBatch:
    """One pass's promotable gaps, collected across all three promoting phases.

    Replaces ``PromotionBudget`` (#4776): with no per-gap ticket there is nothing left
    to ration, so gaps are simply COLLECTED as each phase discovers them via
    :meth:`consider`, and :func:`promote_batch` mints exactly one ticket — or none —
    once every phase has run.
    """

    pending: list[GapSpec] = field(default_factory=list)
    withheld: int = 0
    already_covered: int = 0

    def consider(self, *, gap: GapSpec, dry_run: bool = False) -> ConsiderOutcome:
        """Ground/withhold/dedup one gap and queue it for this pass's batch ticket.

        Mirrors ``promote_gap``'s banned-term/bare-reference withholding, but never
        itself writes the umbrella or schedules anything — that happens ONCE, in
        :func:`promote_batch`, after every phase has considered its gaps, so N
        promoting phases collapse into ONE ticket rather than N.

        Coverage (:func:`gap_covered`) is decided from TICKET state alone, never from
        whether the umbrella body happens to carry this gap's marker: a checkbox line
        can outlive the ticket that added it (a reconciled batch that DROPPED this
        gap leaves its unchecked line sitting there), and re-offering that gap must
        not read a stale line as "still in flight" forever. Re-adding an
        already-present line is harmless — :func:`promote_batch` dedupes by marker.
        """
        safe_title = neutralize_bare_references(gap.title.strip())
        safe_detail = neutralize_bare_references(gap.detail.strip())
        reason = _withholding_reason(f"{safe_title}\n{safe_detail}")
        if reason:
            self.withheld += 1
            # The banned-terms ruleset is versioned: a title promoted last night can be
            # withheld tonight while its checkbox/ticket from that promotion stays live —
            # so coverage is still reported here, not dropped just because this pass
            # cannot (re)write the title (mirrors the old ``promote_gap``'s
            # ``already_present`` computed inside its own withheld branch).
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
                gap_key=gap.gap_key, queued=False, already_covered=True, reason="already covered by an in-flight batch"
            )
        self.pending.append(
            GapSpec(gap_key=gap.gap_key, title=safe_title, cluster_key=gap.cluster_key, detail=safe_detail)
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
    return list(Ticket.objects.exclude(extra__dream_gap_batch__isnull=True))


def gap_covered(gap_key: str) -> bool:
    """Whether *gap_key* already rides an in-flight or delivered ticket — batch or legacy.

    A gap is covered while its batch ticket has not yet been reconciled (still in
    flight, or merged but not yet reconciled) — and, once reconciled, only if it was
    among the gaps actually DELIVERED. A reconciled ticket that DROPPED this gap
    reports it uncovered, so :meth:`PromotionBatch.consider` re-offers it next pass —
    into a NEW batch ticket, so a re-offered gap's key can end up listed in more than
    one ticket's manifest over time (the old one that dropped it, the new one that
    covers it). Every matching ticket is checked, never just the first one found —
    an unordered queryset scan that stopped at the first match could hit a stale
    dropped ticket before the fresh in-flight one and wrongly report "uncovered"
    while a duplicate promotion is actually in flight.
    A gap already scheduled under the OLD per-gap scheme (``dream_gap_key``, still
    unreconciled) is covered too, so a legacy in-flight ticket is never duplicated
    into a fresh batch.
    """
    for ticket in _batch_tickets():
        extra = ticket.extra or {}
        keys = {entry.get("gap_key") for entry in extra.get(_BATCH_KEY) or [] if isinstance(entry, dict)}
        if gap_key not in keys:
            continue
        if not extra.get(_RECONCILED_KEY):
            return True
        if gap_key in set(extra.get(_CLAIMED_DELIVERED_KEY) or []):
            return True
        # else: this ticket DROPPED the gap — keep scanning; a later ticket may cover it.
    return Ticket.objects.filter(extra__dream_gap_key=gap_key, extra__dream_gap_reconciled_at__isnull=True).exists()


def _batch_key(gap_keys: "list[str]") -> str:
    return hashlib.sha256("\n".join(sorted(gap_keys)).encode()).hexdigest()[:16]


def _batch_issue_url(umbrella_url: str, gap_keys: "list[str]") -> str:
    """A unique synthetic issue URL anchoring this PASS's batch Ticket.

    Keyed on the sorted set of pending gap keys (not a timestamp), so re-running the
    SAME pending set — a retried tick, a pass that raised after minting — reuses the
    same ticket via ``get_or_create`` rather than minting a second one for identical
    work.
    """
    return f"{umbrella_url}#dream-batch={_batch_key(gap_keys)}"


def _batch_short_description(gaps: "list[GapSpec]") -> str:
    lead = gaps[0].title.strip()[:40]
    return f"Dream batch: {len(gaps)} gap(s) — {lead}"[:80]


def _gap_manifest_lines(gap: GapSpec) -> "list[str]":
    """One gap's manifest entry: its elided title, plus the full rule when title cut it.

    ``title`` may be elided to a checkbox-sized snippet (:func:`elided_snippet`) or
    cut to its first sentence, dropping the actionable half of a long/multi-sentence
    rule — the coder fixing this gap needs the whole thing, not the fragment a public
    checkbox can afford to show. A substring check (not equality) against the WRAPPED
    title, since ``title`` always carries a label prefix ``detail`` never does.
    """
    title = gap.title.strip()
    lines = [f"- [{gap.gap_key}] {title}"]
    full = gap.detail.strip()
    if full and full not in title:
        lines.append(f"  Full: {full}")
    return lines


def _batch_context(umbrella_url: str, gaps: "list[GapSpec]") -> str:
    """The manifest the dispatched coder reads — every gap, and the delivery contract."""
    lines = [
        "Dream promotion batch (#4776) — fix each gap below independently.",
        (
            "Drop any gap you cannot deliver rather than stretching the change to cover it; "
            "an omitted gap stays open and is re-offered next pass."
        ),
        f"Umbrella ledger: {umbrella_url}",
        "",
        "Gaps in this batch:",
    ]
    for gap in gaps:
        lines.extend(_gap_manifest_lines(gap))
    lines.extend(
        [
            "",
            (
                "Before shipping, record exactly which gaps you delivered — the review phase "
                "verifies each one independently (reproduce-before, absent-after) and holds on "
                "any claimed gap it cannot confirm:"
            ),
            "    ticket.merge_extra(set_keys={'dream_gap_claimed_delivered': [<gap_key>, ...]})",
        ]
    )
    return "\n".join(lines)


def _upsert_gap_checkboxes(host: CodeHostBackend, *, umbrella_url: str, gaps: "list[GapSpec]") -> int:
    """Append every gap's checkbox to the umbrella in ONE read + ONE write, not N.

    Mirrors ``upsert_gap_checkbox`` per gap, but batches the read/write: an N-gap
    pass costs one forge round-trip, not N (a 300-gap pass calling the per-gap
    primitive re-reads and re-scans the whole, ever-growing body 300 times). An
    unreadable body writes nothing, mirroring the per-gap primitive's never-crash
    contract. Returns how many NEW lines were appended.
    """
    body = _read_body(host, umbrella_url)
    if body is None:
        return 0
    lines = body.splitlines()
    added = 0
    for gap in gaps:
        if _line_index(lines, gap.gap_key) != -1:
            continue
        lines.append(render_checkbox_line(gap_key=gap.gap_key, title=gap.title, checked=False))
        added += 1
    if added == 0:
        return 0
    return added if _scrubbed_update(host, umbrella_url=umbrella_url, body="\n".join(lines) + "\n") else 0


def _schedule_batch_fix(*, umbrella_url: str, gaps: "list[GapSpec]") -> Task | None:
    """Mint (or reuse) the ONE Ticket carrying every gap in *gaps*, then schedule coding."""
    gap_keys = [gap.gap_key for gap in gaps]
    issue_url = _batch_issue_url(umbrella_url, gap_keys)
    manifest = [{"gap_key": gap.gap_key, "cluster_key": gap.cluster_key} for gap in gaps]
    ticket, _ = Ticket.objects.get_or_create(
        issue_url=issue_url,
        defaults={
            "role": Ticket.Role.AUTHOR,
            "short_description": _batch_short_description(gaps),
            "context": _batch_context(umbrella_url, gaps),
        },
    )
    ticket.merge_extra(set_keys={_BATCH_KEY: manifest, _UMBRELLA_KEY: umbrella_url})
    if Task.objects.pending_in_phase("coding").filter(ticket=ticket).exists():
        return None
    if ticket.state != Ticket.State.NOT_STARTED:
        return None
    return ticket.schedule_coding()


def promote_batch(
    host: CodeHostBackend, *, umbrella_url: str, batch: PromotionBatch, dry_run: bool = False
) -> BatchOutcome:
    """Mint exactly ONE ticket for everything this pass's phases queued (#4776).

    An empty batch mints nothing — the required negative control: zero pending gaps
    means zero tickets. Otherwise every pending gap gets its umbrella checkbox
    upserted (one rewrite of the umbrella body covers the whole batch, not one write
    per gap) and ONE coding task is scheduled carrying the full manifest.
    """
    if not batch.pending:
        return BatchOutcome(scheduled=False, ticket_url="", gap_count=0, reason="nothing pending")
    if dry_run:
        return BatchOutcome(scheduled=False, ticket_url="", gap_count=len(batch.pending), reason="DRY (no writes)")

    added = _upsert_gap_checkboxes(host, umbrella_url=umbrella_url, gaps=batch.pending)
    task = _schedule_batch_fix(umbrella_url=umbrella_url, gaps=batch.pending)
    return BatchOutcome(
        scheduled=task is not None,
        ticket_url=umbrella_url,
        gap_count=len(batch.pending),
        checkboxes_added=added,
        reason="promoted" if task is not None else "already scheduled",
    )


def _pending_reconcile_batch_tickets() -> "list[Ticket]":
    return list(
        Ticket.objects.exclude(extra__dream_gap_batch__isnull=True).filter(extra__dream_gap_reconciled_at__isnull=True)
    )


def reconcile_batches(host: CodeHostBackend, *, umbrella_url: str) -> "list[Ticket]":
    """Check + retire only the DELIVERED gaps of every MERGED batch ticket (#4776).

    Reads ``dream_gap_claimed_delivered`` (the coder/reviewer-recorded subset),
    intersected with the ticket's own manifest so an out-of-manifest key is never
    trusted. Each delivered gap's checkbox is checked and its memory stamped via the
    existing umbrella-ledger helpers, verbatim. The ticket is stamped reconciled only
    once EVERY delivered gap's checkbox was confirmed checked — mirroring
    ``reconcile_merged_gaps``'s all-or-nothing-per-attempt semantics — so a partial
    forge failure retries the whole ticket next pass rather than leaving a gap's box
    permanently unconfirmed. A gap the coder dropped is left untouched: its checkbox
    stays unchecked and :func:`gap_covered` reports it uncovered, so the next pass's
    ``consider()`` re-offers it.
    """
    from teatree.loops.dream.promote_memory import retire_resolved_memories  # noqa: PLC0415 — tick-time import

    reconciled: list[Ticket] = []
    merged_memory_urls: set[str] = set()
    for ticket in _pending_reconcile_batch_tickets():
        if ticket.state != Ticket.State.MERGED:
            continue
        extra = ticket.extra or {}
        manifest = {
            entry["gap_key"]: entry
            for entry in extra.get(_BATCH_KEY) or []
            if isinstance(entry, dict) and entry.get("gap_key")
        }
        delivered_keys = set(extra.get(_CLAIMED_DELIVERED_KEY) or []) & manifest.keys()
        merged_url = _merged_pr_url(ticket)
        all_confirmed = True
        for gap_key in delivered_keys:
            if not _ensure_gap_checked(host, umbrella_url=umbrella_url, gap_key=gap_key).is_checked:
                all_confirmed = False
                logger.warning(
                    "dream batch reconcile: could not check umbrella box for gap %r — retrying next pass", gap_key
                )
                continue
            cluster_key = str(manifest[gap_key].get("cluster_key") or gap_key)
            if _stamp_memory_merged(cluster_key, merged_url=merged_url):
                merged_memory_urls.add(merged_url)
        if not all_confirmed:
            continue
        _stamp_ticket_reconciled(ticket)
        reconciled.append(ticket)
    if merged_memory_urls:
        retire_resolved_memories(host, is_resolved=lambda url: url in merged_memory_urls)
    return reconciled


__all__ = [
    "BatchOutcome",
    "ConsiderOutcome",
    "PromotionBatch",
    "gap_covered",
    "promote_batch",
    "reconcile_batches",
]
