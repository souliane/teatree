"""Retro's synchronous entry into the dream gap drain — a lesson lands as a scheduled fix.

Nothing here re-implements the drain. A finding is recorded as a
:class:`~teatree.core.models.ConsolidatedMemory` row already classified a core gap,
then promoted as a batch of one through
:mod:`teatree.loops.dream.batch_promote`, so the coverage dedup, the banned-term /
bare-reference withhold and the send-proxy leak gate all come with it — and the row
retires through :func:`~teatree.loops.dream.promote_memory.retire_resolved_memories`
when :func:`~teatree.loops.dream.batch_promote.reconcile_batches` sees the fix merge,
with no retro-specific code anywhere in that path.

The umbrella write sits behind the ``memory_promote`` toggle Pass 2 obeys: with it off
the finding is still RECORDED and its promotion DEFERRED onto the same drain queue, so
nothing is lost by waiting and an explicit command may not spend an intent the operator
withheld.

The row is stamped TICKETED with the batch's promotion anchor by the batch path itself,
so it leaves the drain queue the moment it is promoted, exactly as a Pass-2 gap does.
"""

import hashlib
import re
from dataclasses import dataclass

from django.db import transaction

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConsolidatedMemory
from teatree.loops.dream import batch_promote, umbrella_ledger
from teatree.loops.dream._shared import WEIGHT_RETRO
from teatree.loops.dream.destination import points_at_core_fix
from teatree.loops.dream.loop import memory_promote_enabled
from teatree.loops.dream.promote_memory import UMBRELLA_ISSUE_URL

_WHITESPACE_RE = re.compile(r"\s+")

_TITLE_PREFIX = "Workflow gap (retro finding)"


@dataclass(frozen=True, slots=True)
class FindingOutcome:
    gap_key: str
    checkbox_added: bool
    scheduled: bool
    withheld: bool = False
    deferred: bool = False
    reason: str = ""


def finding_cluster_key(rule: str) -> str:
    """The ledger identity of a finding — over the normalized rule, never the session.

    Content-keyed so re-emitting a lesson whose fix is still open dedups onto the
    existing gap instead of scheduling a second fix for the same rule.
    """
    normalized = _WHITESPACE_RE.sub(" ", rule).strip().lower()
    return hashlib.sha256(normalized.encode()).hexdigest()


def finding_gap(rule: str) -> umbrella_ledger.GapSpec:
    """The umbrella gap *rule* promotes to — derivable without touching the ledger."""
    key = finding_cluster_key(rule)
    return umbrella_ledger.GapSpec(gap_key=key, title=umbrella_ledger.gap_title(_TITLE_PREFIX, rule), cluster_key=key)


def record_finding(*, rule: str, citation: str, destination: str) -> ConsolidatedMemory:
    """Record one finding as a VERIFIED core-gap row, idempotent on the cluster key.

    An empty citation raises and rolls the row back — an uncited rule is a
    hallucinated lesson, and a CANDIDATE row left behind would have no drain. A
    destination outside teatree's own fix paths raises too: the row is stamped
    CORE_GAP unconditionally, so accepting a home Pass-2 triage reads as
    user-specific would make that stamp a lie.
    """
    if not points_at_core_fix(destination):
        msg = f"a finding's destination must name a teatree fix path (skills/, src/teatree, …), not {destination!r}"
        raise ValueError(msg)
    with transaction.atomic():
        row = ConsolidatedMemory.record_cluster(
            cluster_key=finding_cluster_key(rule),
            rule=rule.strip(),
            source_files=[],
            member_count=1,
            max_member_weight=WEIGHT_RETRO,
            is_binding=False,
            durable_destination=destination.strip(),
        )
        if row.status == ConsolidatedMemory.Status.CANDIDATE:
            row.mark_verified(citation)
        if row.disposition == ConsolidatedMemory.Disposition.UNTRIAGED:
            row.classify_core_gap()
    return row


def promote_finding(
    host: CodeHostBackend | None, *, rule: str, umbrella_url: str = UMBRELLA_ISSUE_URL, dry_run: bool = False
) -> FindingOutcome:
    """Drive one finding onto the umbrella + a scheduled coding fix, as a batch of one.

    Deferred rather than promoted whenever the umbrella channel is unavailable —
    ``memory_promote`` off, or no code host reaching the umbrella's forge. Both leave
    the recorded row in the drain queue, so an installation that cannot reach the
    public tracker still loses no lesson.
    """
    if not memory_promote_enabled():
        return _deferred(rule, "deferred — [loops.dream] memory_promote is off; the recorded gap drains once it is on")
    if host is None:
        return _deferred(rule, f"deferred — no code host reaches {umbrella_url}; the recorded gap drains once one does")
    batch = batch_promote.PromotionBatch()
    considered = batch.consider(gap=finding_gap(rule), dry_run=dry_run)
    if not considered.queued:
        return FindingOutcome(
            gap_key=considered.gap_key,
            checkbox_added=False,
            scheduled=False,
            withheld=considered.withheld,
            reason=considered.reason,
        )
    promoted = batch_promote.promote_batch(host, umbrella_url=umbrella_url, batch=batch, dry_run=dry_run)
    return FindingOutcome(
        gap_key=considered.gap_key,
        checkbox_added=promoted.checkboxes_added > 0,
        scheduled=promoted.scheduled,
        reason=promoted.reason,
    )


def _deferred(rule: str, reason: str) -> FindingOutcome:
    return FindingOutcome(
        gap_key=finding_cluster_key(rule),
        checkbox_added=False,
        scheduled=False,
        deferred=True,
        reason=reason,
    )


__all__ = ["FindingOutcome", "finding_cluster_key", "finding_gap", "promote_finding", "record_finding"]
