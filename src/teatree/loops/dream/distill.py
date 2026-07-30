"""Batch the dream distiller so an oversized member set never under-clusters (#1933).

The engine used to hand the entire (oversized) :class:`ConsolidationExtract` to
the distiller in ONE call. An oversized or failed call returns empty/malformed
JSON, which the engine's defensive parse swallows silently — a month-wide run
recorded 0 clusters from 4153 members while a tight 781-member window distilled
3 clusters fine.

:func:`distill_in_batches` splits the weight-sorted member set into tractable
batches (capped by ``T3_DREAM_MAX_DISTILL_MEMBERS``), distils each, and merges
the per-batch clusters by ``cluster_key`` — the ledger's idempotency anchor, so
a key surfaced in two batches collapses to one row instead of duplicating. A
batch that returns 0 clusters from a NON-empty member set is counted and logged
at WARNING so the silent-empty case is surfaced, never swallowed.
"""

import logging
import os
from dataclasses import dataclass

from teatree.loops.dream.engine import ConsolidationExtract, DistilledCluster, Distiller, DistillResult

logger = logging.getLogger(__name__)

#: The distiller is handed at most this many members per call. A single
#: oversized call returns empty/malformed JSON (4153 members produced 0
#: clusters; 781 distilled fine), so the weighted member set is split into
#: batches no larger than this cap. Overridable via ``T3_DREAM_MAX_DISTILL_MEMBERS``.
_DEFAULT_MAX_DISTILL_MEMBERS = 400
_MAX_DISTILL_MEMBERS_ENV = "T3_DREAM_MAX_DISTILL_MEMBERS"


@dataclass(frozen=True, slots=True)
class BatchDistillOutcome:
    """The merged result of distilling an extract batch-by-batch.

    ``clusters`` are deduplicated by ``cluster_key`` across batches (the ledger
    upserts by that key, so a key surfaced in two batches must collapse to one).
    ``empty_batches`` counts batches that returned 0 clusters from a NON-empty
    member set — the silent-empty signal the engine must surface, never swallow.
    ``failed_batches`` counts batches whose distiller call RAISED — isolated per batch
    so one batch's failure never discards the clusters already distilled from the
    others (paid LLM work), and the count surfaces the partial failure.
    """

    clusters: list[DistilledCluster]
    empty_batches: int
    failed_batches: int = 0


def _max_distill_members() -> int:
    raw = os.environ.get(_MAX_DISTILL_MEMBERS_ENV, "").strip()
    if not raw:
        return _DEFAULT_MAX_DISTILL_MEMBERS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_DISTILL_MEMBERS
    return value if value > 0 else _DEFAULT_MAX_DISTILL_MEMBERS


def _batch_extracts(extract: ConsolidationExtract, max_members: int) -> list[ConsolidationExtract]:
    """Split the weight-sorted snippets into batches no larger than *max_members*.

    Snippets are already ordered highest-weight first by ``build_extract``, so the
    first batches carry the highest-signal members. The ``truncated`` flag rides on
    the final batch only.
    """
    snippets = extract.snippets
    if not snippets:
        return []
    batches: list[ConsolidationExtract] = []
    for start in range(0, len(snippets), max_members):
        chunk = snippets[start : start + max_members]
        is_last = start + max_members >= len(snippets)
        batches.append(ConsolidationExtract(snippets=chunk, truncated=extract.truncated and is_last))
    return batches


def distill_in_batches(extract: ConsolidationExtract, *, distiller: Distiller) -> BatchDistillOutcome:
    """Distil *extract* batch-by-batch, merging clusters by ``cluster_key``.

    Each batch is at most ``T3_DREAM_MAX_DISTILL_MEMBERS`` members so a single
    oversized call can never silently return nothing. Clusters are merged
    last-wins by ``cluster_key`` (the ledger's idempotency anchor), so a key
    surfaced in two batches collapses to one row instead of duplicating. A batch
    that returns 0 clusters from a NON-empty member set is counted and logged with
    the distiller's :class:`~teatree.loops.dream.engine.DistillEmptyReason` so the
    operator can tell a healthy no-consolidation from a broken parse (#2847) — the
    silent-empty case is surfaced with WHY, never swallowed.

    Each batch's distiller call is fault-ISOLATED (F6.4): a batch that RAISES is
    logged, counted in ``failed_batches``, and skipped, so one oversized/failed batch
    never discards the clusters already distilled from the earlier batches (paid LLM
    work). The failure count rides the outcome so a partial distillation is surfaced,
    not silently reported as fewer clusters.
    """
    merged: dict[str, DistilledCluster] = {}
    empty_batches = 0
    failed_batches = 0
    for batch in _batch_extracts(extract, _max_distill_members()):
        try:
            result = _as_result(distiller(batch))
        except Exception:
            failed_batches += 1
            logger.warning(
                "dream distiller RAISED on a batch of %d member(s) — skipping it, keeping the "
                "clusters already distilled from the other batches.",
                len(batch.snippets),
                exc_info=True,
            )
            continue
        if not result.clusters:
            empty_batches += 1
            reason = f" — reason: {result.empty_reason.value}" if result.empty_reason else ""
            logger.warning(
                "dream distiller returned 0 clusters from a non-empty batch of %d member(s)%s",
                len(batch.snippets),
                reason,
            )
            continue
        for cluster in result.clusters:
            merged[cluster.cluster_key] = cluster
    return BatchDistillOutcome(
        clusters=list(merged.values()), empty_batches=empty_batches, failed_batches=failed_batches
    )


def _as_result(returned: list[DistilledCluster] | DistillResult) -> DistillResult:
    """Normalize a distiller return: the real distiller carries a reason, a fake may not."""
    if isinstance(returned, DistillResult):
        return returned
    return DistillResult(clusters=returned, empty_reason=None)


__all__ = ["BatchDistillOutcome", "distill_in_batches"]
