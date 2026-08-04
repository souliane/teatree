"""Batch the dream distiller so the whole corpus is consolidated, not just one prompt.

This module owns the PROMPT BUDGET. :func:`~teatree.loops.dream.engine.build_extract`
ranks every replayed member and discards none; :func:`distill_in_batches` partitions
that ranking into calls of at most ``ConsolidationExtract.CHAR_CEILING`` characters
and ``T3_DREAM_MAX_DISTILL_MEMBERS`` members. Holding the budget here rather than at
extract time is what makes a corpus larger than one prompt reachable at all: bounding
the corpus instead meant a machine with 419 members and 2.9M characters fed 20
snippets to a single call and permanently discarded the other 399, every pass.

``T3_DREAM_MAX_DISTILL_BATCHES`` bounds the calls ONE pass may make. When it binds,
the batches it could not reach are not lost: the first batch (the ranking's head, so
the curated doctrine and the freshest drift) is always distilled, the rest are walked
in rotation from a cursor persisted on ``DreamRunMarker``, and the count left over is
reported as ``deferred_members``. Successive passes therefore drain the corpus instead
of re-distilling the same head forever.

A batch that RAISES and a batch whose reply is BROKEN (an unauthenticated ``claude``
answering ``Not logged in · Please run /login``, a truncated array, entries that all
fail to coerce) are both counted apart from a healthy "nothing to consolidate" and
carry a diagnostic naming the reason and the reply. The engine turns either into a
FAILED pass — a consolidation that did not happen must never be reported as a quiet
night.
"""

import logging
import os
from dataclasses import dataclass, field

from teatree.loops.dream.engine import ConsolidationExtract, DistilledCluster, Distiller, DistillResult, WeightedSnippet

logger = logging.getLogger(__name__)

#: Members per distiller call. Bounds the batch alongside the character ceiling —
#: whichever binds first splits the batch. Overridable via the env var below.
_DEFAULT_MAX_DISTILL_MEMBERS = 400
_MAX_DISTILL_MEMBERS_ENV = "T3_DREAM_MAX_DISTILL_MEMBERS"

#: Distiller calls ONE pass may make, bounding an unattended pass's wall clock and
#: spend on a corpus of any size. ``0`` means unlimited. Whatever the cap leaves
#: unreached is carried to the next pass, never dropped.
_DEFAULT_MAX_DISTILL_BATCHES = 24
_MAX_DISTILL_BATCHES_ENV = "T3_DREAM_MAX_DISTILL_BATCHES"

#: How much of a broken reply rides the diagnostic. Enough to read a refusal or an
#: auth error in full; short enough that a wall of malformed JSON cannot flood the log.
_RAW_EXCERPT_CHARS = 400


@dataclass(frozen=True, slots=True)
class BatchDistillOutcome:
    """The merged result of distilling an extract batch-by-batch.

    ``clusters`` are deduplicated by ``cluster_key`` across batches (the ledger
    upserts by that key, so a key surfaced in two batches must collapse to one).
    ``empty_batches`` counts every batch that returned 0 clusters from a NON-empty
    member set; ``broken_batches`` counts the subset of those whose empty reason means
    the distiller could not do its job, and ``failed_batches`` those whose call raised.
    A failure is isolated per batch, so one bad call never discards the clusters already
    distilled from the others (paid LLM work). ``diagnostics`` carries one
    human-readable line per failed / broken batch. ``deferred_members`` counts snippets
    the per-pass batch cap could not reach, which the cursor carries forward.
    """

    clusters: list[DistilledCluster]
    empty_batches: int
    failed_batches: int = 0
    broken_batches: int = 0
    diagnostics: tuple[str, ...] = ()
    snippets_distilled: int = 0
    deferred_members: int = 0


@dataclass(slots=True)
class _BatchTally:
    """Mutable accumulator for one pass over the selected batches."""

    merged: dict[str, DistilledCluster] = field(default_factory=dict)
    empty: int = 0
    failed: int = 0
    broken: int = 0
    diagnostics: list[str] = field(default_factory=list)
    distilled_snippets: int = 0


def _positive_env_int(name: str, default: int, *, zero_allowed: bool = False) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value > 0 or (zero_allowed and value == 0):
        return value
    return default


def _max_distill_members() -> int:
    return _positive_env_int(_MAX_DISTILL_MEMBERS_ENV, _DEFAULT_MAX_DISTILL_MEMBERS)


def _max_distill_batches() -> int:
    return _positive_env_int(_MAX_DISTILL_BATCHES_ENV, _DEFAULT_MAX_DISTILL_BATCHES, zero_allowed=True)


def _batch_extracts(extract: ConsolidationExtract, *, max_members: int, max_chars: int) -> list[ConsolidationExtract]:
    """Partition the ranked snippets into calls bounded by BOTH members and characters.

    Rank order is preserved, so batch 0 holds the head of the ranking — the curated
    doctrine and freshest drift ``build_extract`` placed there. A single snippet larger
    than *max_chars* still forms its own batch rather than being dropped; the engine's
    per-member caps already bound how large that can be.
    """
    batches: list[ConsolidationExtract] = []
    chunk: list[WeightedSnippet] = []
    chars = 0
    for snippet in extract.snippets:
        if chunk and (len(chunk) >= max_members or chars + len(snippet.text) > max_chars):
            batches.append(ConsolidationExtract(snippets=tuple(chunk)))
            chunk, chars = [], 0
        chunk.append(snippet)
        chars += len(snippet.text)
    if chunk:
        batches.append(ConsolidationExtract(snippets=tuple(chunk)))
    return batches


def _select_batches(total: int, *, cap: int, cursor: int) -> tuple[list[int], int]:
    """Which batch indices this pass distils, and the cursor the next pass resumes from.

    Batch 0 carries the doctrine and the freshest drift, so a pass with room for more
    than one call always spends one on it and rotates the rest of the corpus through
    the remaining budget. A cap of ONE is the exception: pinning the single call to the
    head would mean the tail is never consolidated at all — the very defect the cursor
    exists to remove — so at that cap everything rotates, batch 0 included. The cursor
    is an offset into whichever region rotates; the modulo keeps a cursor written under
    one cap valid under another.
    """
    if cap <= 0 or cap >= total:
        return list(range(total)), 0
    head = [0] if cap > 1 else []
    rotation = range(len(head), total)
    start = cursor % len(rotation)
    take = min(cap - len(head), len(rotation))
    picked = [rotation[(start + step) % len(rotation)] for step in range(take)]
    return head + picked, (start + take) % len(rotation)


def _read_cursor() -> int:
    from teatree.core.models import DreamRunMarker  # noqa: PLC0415 — deferred: ORM import needs the app registry

    marker = DreamRunMarker.objects.filter(name=DreamRunMarker.NAME).first()
    return marker.distill_cursor if marker else 0


def _write_cursor(cursor: int) -> None:
    from teatree.core.models import DreamRunMarker  # noqa: PLC0415 — deferred: ORM import needs the app registry

    DreamRunMarker.objects.update_or_create(name=DreamRunMarker.NAME, defaults={"distill_cursor": cursor})


def distill_in_batches(
    extract: ConsolidationExtract, *, distiller: Distiller, dry_run: bool = False
) -> BatchDistillOutcome:
    """Distil *extract* batch-by-batch, merging clusters by ``cluster_key``.

    Every ranked snippet reaches a call unless the per-pass batch cap binds, in which
    case the unreached remainder is counted in ``deferred_members`` and the cursor
    advances so the NEXT pass continues from there. Under *dry_run* the cursor is left
    untouched, so a preview never consumes the corpus.

    Clusters merge last-wins by ``cluster_key`` (the ledger's idempotency anchor), so a
    key surfaced in two batches collapses to one row. A batch that raises or returns a
    broken reply is logged, counted, given a diagnostic, and skipped — never allowed to
    discard the clusters the other batches already produced.
    """
    batches = _batch_extracts(extract, max_members=_max_distill_members(), max_chars=ConsolidationExtract.CHAR_CEILING)
    if not batches:
        return BatchDistillOutcome(clusters=[], empty_batches=0)

    cap = _max_distill_batches()
    capped = 0 < cap < len(batches)
    selected, next_cursor = _select_batches(len(batches), cap=cap, cursor=_read_cursor() if capped else 0)

    tally = _BatchTally()
    for index in selected:
        _distil_one(batches[index], distiller=distiller, tally=tally)
    if capped and not dry_run:
        _write_cursor(next_cursor)

    reached = set(selected)
    return BatchDistillOutcome(
        clusters=list(tally.merged.values()),
        empty_batches=tally.empty,
        failed_batches=tally.failed,
        broken_batches=tally.broken,
        diagnostics=tuple(tally.diagnostics),
        snippets_distilled=tally.distilled_snippets,
        deferred_members=sum(len(batch.snippets) for i, batch in enumerate(batches) if i not in reached),
    )


def _distil_one(batch: ConsolidationExtract, *, distiller: Distiller, tally: _BatchTally) -> None:
    size = len(batch.snippets)
    tally.distilled_snippets += size
    try:
        result = _as_result(distiller(batch))
    except Exception as exc:
        tally.failed += 1
        tally.diagnostics.append(f"batch of {size} member(s) RAISED {type(exc).__name__}: {exc}")
        logger.warning(
            "dream distiller RAISED on a batch of %d member(s) — skipping it, keeping the "
            "clusters already distilled from the other batches.",
            size,
            exc_info=True,
        )
        return
    if not result.clusters:
        _tally_empty(result, size=size, tally=tally)
        return
    for cluster in result.clusters:
        tally.merged[cluster.cluster_key] = cluster


def _tally_empty(result: DistillResult, *, size: int, tally: _BatchTally) -> None:
    tally.empty += 1
    reason = result.empty_reason
    if reason is not None and reason.is_broken:
        tally.broken += 1
        excerpt = result.raw_excerpt.strip()[:_RAW_EXCERPT_CHARS]
        tally.diagnostics.append(
            f"batch of {size} member(s): {reason.value}" + (f" — reply: {excerpt!r}" if excerpt else "")
        )
    logger.warning(
        "dream distiller returned 0 clusters from a non-empty batch of %d member(s)%s",
        size,
        f" — reason: {reason.value}" if reason else "",
    )


def _as_result(returned: list[DistilledCluster] | DistillResult) -> DistillResult:
    """Normalize a distiller return: the real distiller carries a reason, a fake may not."""
    if isinstance(returned, DistillResult):
        return returned
    return DistillResult(clusters=returned, empty_reason=None)


__all__ = ["BatchDistillOutcome", "distill_in_batches"]
