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

A COUNT cap alone cannot bound the pass's wall clock, and that is what killed every
pass: 24 batches against the distiller's own 300s per-call watchdog is up to two hours
of metered work inside a 30-minute pass budget, so the pass never ended by choice —
only by the driver's SIGKILL, always mid-distil, leaving the whole tail (compliance,
the §4 acceptance gates, phases 4-6, Pass-2 promotion, the marker) unreachable. So
:func:`distill_in_batches` also takes an optional
:class:`~teatree.loops.dream.pass_config.PassBudget` and stops launching NEW batches
once the remaining budget can no longer absorb one worst-case call AND still leave the
tail its reserve. This changes only WHEN distil stops, never what happens to the
remainder: the same rotation cursor that carries the batch cap's leftovers carries the
clock's, so the next pass resumes exactly where this one stopped.

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

from teatree.loops.dream.engine import DistilledCluster, Distiller, DistillResult
from teatree.loops.dream.pass_config import PassBudget
from teatree.loops.dream.replay import ConsolidationExtract, WeightedSnippet
from teatree.loops.dream.sdk_distiller import DISTILL_WATCHDOG_SECONDS

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

    ``next_cursor`` is a PROPOSAL, not a fact: the value the cursor should take once —
    and only once — the clusters this pass produced are durably in the ledger. ``None``
    means the cursor must not move at all. It is deliberately not written here; see
    :func:`distill_in_batches`.

    ``budget_stopped_batches`` counts the SELECTED batches the wall-clock budget turned
    away, told apart from the ones the count cap never selected. A pass that stops on
    the clock and says nothing about it reads identically to a pass that finished, which
    is how a 30-minute overrun stayed invisible for weeks.

    ``rotation_len`` and ``rotation_advance`` describe the SWEEP the cursor is walking —
    how many batches rotate and how many this pass consumed. A deferral percentage alone
    reads as a queue that never drains ("91% deferred every night"); with these the same
    pass reports how many passes a full sweep takes, which is what says whether the
    rotation converges (#4671).
    """

    clusters: list[DistilledCluster]
    empty_batches: int
    failed_batches: int = 0
    broken_batches: int = 0
    diagnostics: tuple[str, ...] = ()
    snippets_distilled: int = 0
    deferred_members: int = 0
    next_cursor: int | None = None
    budget_stopped_batches: int = 0
    rotation_len: int = 0
    rotation_advance: int = 0

    @property
    def sweep_passes(self) -> int:
        """Passes this rotation needs to visit every batch once, 0 when it cannot advance."""
        if self.rotation_len <= 0 or self.rotation_advance <= 0:
            return 0
        return -(-self.rotation_len // self.rotation_advance)


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


@dataclass(frozen=True, slots=True)
class _BatchSelection:
    """Which batch indices this pass distils, and where the next pass resumes.

    ``indices`` is the optional head (batch 0 — the doctrine and the freshest drift)
    followed by the rotation window. The cursor used to be a single number computed at
    selection time, which was only correct while EVERY selected batch was guaranteed to
    run. The wall-clock budget breaks that guarantee, so the resume point is a FUNCTION
    of how many of ``indices`` were actually reached: :meth:`cursor_after`.
    """

    indices: tuple[int, ...]
    head_len: int
    rotation_len: int
    start: int

    def cursor_after(self, reached: int) -> int:
        """Where the next pass resumes, given the first *reached* of ``indices`` ran.

        Only rotation batches move the cursor — the head is re-distilled every pass by
        design, so reaching it consumes nothing. ``reached == len(indices)`` reproduces
        the value the old selection-time arithmetic returned.
        """
        if self.rotation_len <= 0:
            return 0
        taken = max(0, reached - self.head_len)
        return (self.start + taken) % self.rotation_len


def _select_batches(total: int, *, cap: int, cursor: int) -> _BatchSelection:
    """Which batch indices this pass distils, and how to resume where it stops.

    Batch 0 carries the doctrine and the freshest drift, so a pass with room for more
    than one call always spends one on it and rotates the rest of the corpus through
    the remaining budget. A cap of ONE is the exception: pinning the single call to the
    head would mean the tail is never consolidated at all — the very defect the cursor
    exists to remove — so at that cap everything rotates, batch 0 included. The cursor
    is an offset into whichever region rotates; the modulo keeps a cursor written under
    one cap valid under another.

    An UNCAPPED pass selects every batch, and now walks them from the cursor too. With
    the steady-state cursor of 0 — what a pass that reached every batch leaves behind —
    that is exactly ``range(total)``, unchanged. It matters only when the wall-clock
    budget cut the PREVIOUS pass short: without it the next pass would restart at the
    head and re-spend metered calls on the region already consolidated, never reaching
    the tail of the corpus.
    """
    if cap <= 0 or cap >= total:
        start = cursor % total
        return _BatchSelection(
            indices=tuple((start + step) % total for step in range(total)),
            head_len=0,
            rotation_len=total,
            start=start,
        )
    head = (0,) if cap > 1 else ()
    rotation = range(len(head), total)
    start = cursor % len(rotation)
    take = min(cap - len(head), len(rotation))
    picked = tuple(rotation[(start + step) % len(rotation)] for step in range(take))
    return _BatchSelection(indices=head + picked, head_len=len(head), rotation_len=len(rotation), start=start)


def _read_cursor() -> int:
    from teatree.core.models import DreamRunMarker  # noqa: PLC0415 — deferred: ORM import needs the app registry

    marker = DreamRunMarker.objects.filter(name=DreamRunMarker.NAME).first()
    return marker.distill_cursor if marker else 0


def commit_distill_cursor(cursor: int) -> None:
    """Move the persisted rotation cursor to *cursor*.

    Called by :func:`~teatree.loops.dream.engine.run_consolidation` INSIDE the same
    transaction as the ledger write, never from :func:`distill_in_batches`. The cursor
    is a claim that a region of the corpus has been consolidated, so it may only become
    true at the instant the rows proving it become true.
    """
    from teatree.core.models import DreamRunMarker  # noqa: PLC0415 — deferred: ORM import needs the app registry

    DreamRunMarker.objects.update_or_create(name=DreamRunMarker.NAME, defaults={"distill_cursor": cursor})


def distill_in_batches(
    extract: ConsolidationExtract,
    *,
    distiller: Distiller,
    dry_run: bool = False,
    budget: PassBudget | None = None,
) -> BatchDistillOutcome:
    """Distil *extract* batch-by-batch, merging clusters by ``cluster_key``.

    Every ranked snippet reaches a call unless the per-pass batch cap binds, in which
    case the unreached remainder is counted in ``deferred_members`` and the cursor is
    PROPOSED to advance so the NEXT pass continues from there. Under *dry_run* nothing
    is proposed, so a preview never consumes the corpus.

    *budget* is the pass's wall clock. When given, a new batch is launched only while
    the budget can still absorb one worst-case distiller call (its own
    :data:`~teatree.loops.dream.sdk_distiller.DISTILL_WATCHDOG_SECONDS` watchdog) AND
    leave the tail its reserve; the first batch that does not fit ends the walk. The
    remainder is deferred through the same cursor the count cap uses, so "the clock
    stopped us" and "the cap stopped us" leave the corpus in the same recoverable
    state. Passing ``None`` restores the unbounded walk (the manual/test path).

    Clusters merge last-wins by ``cluster_key`` (the ledger's idempotency anchor), so a
    key surfaced in two batches collapses to one row. A batch that raises or returns a
    broken reply is logged, counted, given a diagnostic, and skipped — never allowed to
    discard the clusters the other batches already produced.

    The cursor is REPORTED here and COMMITTED by the caller, for two reasons.

    *   **Atomicity.** Writing it here put it in its own autocommit, ahead of
        ``write_clusters``. Anything raising in that window — the ledger write itself,
        the eval proposer, a lost DB connection — left a cursor claiming a region was
        consolidated and no rows to show for it, and that region is not revisited until
        the rotation wraps the entire corpus.
    *   **Conditionality.** A batch that raised or came back broken was NOT consolidated;
        :func:`_distil_one` deliberately swallows both so one bad call cannot discard the
        other batches' paid work, and the cursor advancing anyway turned a single auth
        outage (an unauthenticated ``claude`` answering ``Not logged in``) into a walk of
        the cursor across the whole corpus, skipping all of it, reporting each pass as
        merely "0 clusters". So a pass with any failed or broken batch proposes NO
        advance and the next pass re-reaches the same region.

    Refusing to advance can, in principle, park the rotation on a permanently broken
    batch. That is the strictly better failure: it is loud on every pass
    (``distillation_broken`` carries the reply excerpt into the command's report),
    whereas advancing is silent and consumes the corpus while it does it.
    """
    batches = _batch_extracts(extract, max_members=_max_distill_members(), max_chars=ConsolidationExtract.CHAR_CEILING)
    if not batches:
        return BatchDistillOutcome(clusters=[], empty_batches=0)

    cap = _max_distill_batches()
    capped = 0 < cap < len(batches)
    selection = _select_batches(len(batches), cap=cap, cursor=_read_cursor())

    tally = _BatchTally()
    reached: list[int] = []
    for index in selection.indices:
        if budget is not None and not budget.allows_new_call(DISTILL_WATCHDOG_SECONDS):
            _log_budget_stop(budget, stopped=len(selection.indices) - len(reached), of=len(selection.indices))
            break
        _distil_one(batches[index], distiller=distiller, tally=tally)
        reached.append(index)

    stopped = len(selection.indices) - len(reached)
    consolidated = not tally.failed and not tally.broken
    distilled = set(reached)
    # The cursor must advance whenever a region was left behind, whichever bound left
    # it: a clock-truncated UNCAPPED pass that did not advance would re-distil the same
    # head next pass and never reach the corpus's tail.
    resumable = capped or stopped > 0
    return BatchDistillOutcome(
        clusters=list(tally.merged.values()),
        empty_batches=tally.empty,
        failed_batches=tally.failed,
        broken_batches=tally.broken,
        diagnostics=tuple(tally.diagnostics),
        snippets_distilled=tally.distilled_snippets,
        deferred_members=sum(len(batch.snippets) for i, batch in enumerate(batches) if i not in distilled),
        next_cursor=selection.cursor_after(len(reached)) if resumable and not dry_run and consolidated else None,
        budget_stopped_batches=stopped,
        rotation_len=selection.rotation_len,
        rotation_advance=max(0, len(reached) - selection.head_len),
    )


def _log_budget_stop(budget: PassBudget, *, stopped: int, of: int) -> None:
    """Say loudly that the CLOCK, not the corpus, ended the distil phase."""
    logger.warning(
        "dream pass STOPPED launching distiller batches with %.0fs of its %.0fs budget left — under the "
        "%.0fs tail reserve plus the %.0fs per-call watchdog. %d of %d selected batch(es) are carried to "
        "the next pass by the distill cursor, and the pass's tail (compliance, the acceptance gates, "
        "phases 4-6, the marker) now runs instead of being SIGKILLed mid-distil.",
        budget.remaining,
        budget.total,
        budget.tail_reserve,
        DISTILL_WATCHDOG_SECONDS,
        stopped,
        of,
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
