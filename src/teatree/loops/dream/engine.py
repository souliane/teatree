"""Distillation engine for the idle-time dream pass (#1933).

This module is the single, well-named entry point the dream cron exercises so
the whole orchestration around it — the in-flight lease, the ``--dry-run``
no-write path, ``DreamRunMarker`` stamping, and the staleness alarm — is fully
testable WITHOUT an LLM.

The pass runs in three phases, adapted from arXiv:2606.03979 (schedule + safety
ordering only — no weight updates):

Phase 1 (replay / extract) — :func:`enumerate_members` lists the curated memory
files (``*/memory/*.md`` under ``~/.claude/projects``, re-read regardless of age)
and the recency-gated session transcripts (``*/*.jsonl`` main sessions,
``*/*/subagents/agent-*.jsonl`` sub-agents, ``*/*/tasks/*.output`` task outputs);
:func:`build_extract` reads them, ranks by weight (user-correction
BINDING/``feedback_*`` highest, then retro findings, cold reviews, deny-streaks,
other), keeps only high-signal transcript lines, and bounds the total size hard
so the LLM prompt can never blow up.

Phase 2 (cluster / distill) — the injected :class:`Distiller` groups the extract
by root cause and returns one imperative :class:`DistilledCluster` per group,
each citing a real mistake present in the extract. :func:`distill_in_batches`
splits the weighted member set into tractable batches (capped by
``T3_DREAM_MAX_DISTILL_MEMBERS``) so a single oversized call can never silently
return nothing, then merges the per-batch clusters by ``cluster_key``; a batch
that returns 0 clusters from a NON-empty member set is counted and logged rather
than swallowed. The real distiller (:func:`~teatree.loops.dream.sdk_distiller.
sdk_distiller`, a sibling module — the LLM-call + JSON-parse concern, split out of
this engine in #2723) makes ONE bounded headless ``claude-agent-sdk`` call per batch
(the headless-runner invocation shape) and parses its JSON defensively; tests inject
a fake so the engine needs no LLM. The cluster identity is the deterministic sha256
``cluster_key`` over the member set (not the LLM's prose), so a reworded slug upserts
to one ledger row instead of forking a duplicate.

Phase 3 (write to the ledger) — :func:`write_clusters` rejects any uncited /
unknown-source / blank-citation cluster (no hallucinated memories), idempotently
upserts by ``cluster_key`` via the manager (no duplicate rows on re-run), and
never destructively overwrites a BINDING row's rule.

The output store is the DB-backed :class:`~teatree.core.models.ConsolidatedMemory`
ledger; :func:`write_clusters` reuses its ``record_cluster`` factory rather than
bypassing the manager.

Phase 3b (propose evals — default OFF, #2346) — when ``run_consolidation`` is
given an ``EvalProposalRequest``, the sibling :mod:`teatree.loops.dream.eval_proposer`
maps each grounded cluster to an inert eval CANDIDATE and appends it to a JSONL
review queue. This realises the "a behavioural drift is not fixed until an
anti-vacuous eval pins it" rule from the dreaming side, but only as a CANDIDATE:
a core-maker / human ratifies each into a real ``under_load`` scenario (pollution
preamble + discriminating matchers + ``_pass``/``_fail`` fixtures + the teeth
proof). The engine never autonomously writes a scenario file or a fixture — the
LLM-generated, self-anti-vacuous derivation is the deferred follow-up the design
issue specifies.

The file-side phases over the discovered ``~/.claude`` memory dirs are LIVE
(#1933 § 6, shipped in #2489) and run from the cron command after the pass, each
behind its own kill-switch and fault-isolated: phase 4 cross-link
(:mod:`teatree.loops.dream.cross_link`), phase 5 ``MEMORY.md`` re-index
(:mod:`teatree.loops.dream.reindex`), and phase 6 decay / archive
(:mod:`teatree.loops.dream.decay`). They are invoked by
``teatree.core.management.commands.dream``, not by this engine, which owns only
the transcript-side replay → cluster → distill → ledger pipeline (phases 1-3).

The § 4 acceptance gates (a)-(f) are LIVE (#2545, :mod:`teatree.loops.dream.gates`):
the cron snapshots each memory dir BEFORE and AFTER the file-side phases, runs the
six gates (retention / interference / consolidation-happened / index-budget /
monotonicity / no-loss-audit) and POPULATES the ``DreamQaProbe`` corpus (formerly
a dead model) by replaying a probe derived from each memory's signature line. A
failing gate marks the pass attempted-not-succeeded (staleness keeps firing), so a
lossy / delete-only / no-op consolidation is caught rather than stamped success.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from django.db import transaction

# Phase 1 lives in the sibling `replay` module; its vocabulary is re-exported here so
# every existing `from teatree.loops.dream.engine import ...` caller is unaffected by
# the split, and `__all__` below still names the whole pipeline's surface.
from teatree.loops.dream.replay import (
    ConsolidationExtract,
    TranscriptMember,
    WeightedSnippet,
    build_extract,
    default_projects_dir,
    enumerate_members,
    looks_like_user_correction,
)

if TYPE_CHECKING:
    from teatree.loops.dream.eval_proposer import EvalProposalRequest

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DistilledCluster:
    """One root-cause cluster the distiller returns — a candidate ledger row."""

    cluster_key: str
    rule: str
    source_files: list[str]
    is_binding: bool
    verified_citation: str
    durable_destination: str


class DistillEmptyReason(StrEnum):
    """WHY a distiller produced 0 clusters — so a healthy-0 is told from a broken-0.

    ``NOTHING_TO_CONSOLIDATE`` is the only HEALTHY value (the model returned a real
    empty array — nothing met the consolidation bar). The other three are broken
    output the operator must act on: ``EMPTY_RAW`` (the model returned empty /
    whitespace), ``UNPARSABLE`` (no decodable JSON array in the reply), and
    ``ALL_ENTRIES_DROPPED`` (an array decoded but every element was malformed).
    """

    NOTHING_TO_CONSOLIDATE = "nothing_to_consolidate"
    EMPTY_RAW = "empty_raw"
    UNPARSABLE = "unparsable"
    ALL_ENTRIES_DROPPED = "all_entries_dropped"

    @property
    def is_broken(self) -> bool:
        """True when the distiller could not do its job, as opposed to having nothing to do.

        The distinction decides whether the whole pass failed: a broken reply means the
        consolidation never happened (an unauthenticated ``claude`` answers
        ``Not logged in · Please run /login``, which is non-blank prose carrying no JSON
        array), whereas a genuine empty array means it happened and found nothing.
        """
        return self is not DistillEmptyReason.NOTHING_TO_CONSOLIDATE


@dataclass(frozen=True, slots=True)
class DistillResult:
    """A distiller call's clusters plus, when empty, WHY (#2847).

    ``empty_reason`` is set exactly when ``clusters`` is empty and ``None`` otherwise,
    so :func:`distill_in_batches` can surface a broken parse distinctly from a genuine
    no-consolidation in its 0-cluster WARNING. ``raw_excerpt`` carries a bounded slice
    of the reply behind a BROKEN reason, so the operator reads the actual refusal
    instead of a bare ``unparsable`` they can only explain by instrumenting the code.
    """

    clusters: list[DistilledCluster]
    empty_reason: DistillEmptyReason | None
    raw_excerpt: str = ""


class Distiller(Protocol):
    """The injected LLM seam: extract → root-cause clusters.

    The production distiller returns a :class:`DistillResult` carrying the clusters and,
    when empty, the :class:`DistillEmptyReason` so the 0-cluster path is diagnosable. A
    test fake may return a bare ``list[DistilledCluster]``; :func:`distill_in_batches`
    normalizes both, so a minimal fake never constructs a diagnostic it has no basis for.
    """

    def __call__(self, extract: ConsolidationExtract) -> list[DistilledCluster] | DistillResult: ...


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """The ledger-write tally for one pass: rows written vs ungrounded rows rejected.

    ``rejected`` counts clusters the reject guard dropped (empty / unknown-source /
    invented-quote citations) — the hallucinated-rule shapes the ledger must never
    persist. Each rejection is logged at WARNING by :func:`write_clusters`, so a
    silently-ungrounded distiller batch is surfaced, never swallowed.
    """

    written: int
    rejected: int


@dataclass(frozen=True, slots=True)
class DreamRunResult:
    clusters_recorded: int
    members_replayed: int
    dry_run: bool
    evals_proposed: int = 0
    empty_batches: int = 0
    #: How many snippets the bounded extract actually fed the distiller — the honest
    #: "distilled N snippet(s)" metric (0 clusters from many snippets is a real signal,
    #: not a healthy quiet night).
    snippets_distilled: int = 0
    #: How many candidate clusters the ledger-write reject guard dropped as ungrounded.
    clusters_rejected: int = 0
    #: Batches whose distiller call RAISED, and batches whose reply was BROKEN
    #: (unauthenticated / unparsable / every entry malformed). Either means the
    #: consolidation did not happen, which is why :attr:`distillation_broken` fails
    #: the pass rather than reporting a quiet night.
    failed_batches: int = 0
    broken_batches: int = 0
    #: One line per failed / broken batch, carrying the reason AND the reply excerpt,
    #: so the operator reads ``Not logged in · Please run /login`` on the FAIL line
    #: instead of a bare ``unparsable``.
    distill_diagnostics: tuple[str, ...] = ()
    #: Members a per-pass batch cap could not reach. They are carried to the next pass
    #: via the distill cursor, never dropped; a non-zero count is WARNED so a pass that
    #: saw only part of the corpus says so.
    deferred_members: int = 0
    #: The ranked extract this pass built, so the command can reuse it for the
    #: compliance-measurement and automatable-ask phases instead of re-enumerating +
    #: re-reading every member a second time. ``None`` only on a pass that built none.
    extract: "ConsolidationExtract | None" = None

    @property
    def distillation_broken(self) -> bool:
        """True when the distiller could not do its job on at least one batch."""
        return bool(self.failed_batches or self.broken_batches)


def write_clusters(
    clusters: Sequence[DistilledCluster],
    extract: ConsolidationExtract,
    *,
    dry_run: bool,
    overlay: str = "",
) -> WriteOutcome:
    """Idempotently record valid clusters into the ConsolidatedMemory ledger.

    A cluster is rejected (counted, LOGGED at WARNING, never written) when its
    ``source_files`` is empty, cites a path not present in *extract*, or its
    ``verified_citation`` does not appear (whitespace-normalized substring) in a
    cited snippet's text — these are the hallucinated-rule shapes the ledger must
    never persist, including a real-path-but-invented-quote citation. Each rejection
    is logged so an ungrounded distiller batch is surfaced, not swallowed. A valid
    cluster is upserted by ``cluster_key`` through the manager factory, so a
    re-run that re-clusters the same members updates the row in place instead of
    duplicating it. A BINDING row's ``rule`` is never destructively overwritten.

    Before the reject guard, any MEMORY_ONLY cluster whose rule already shows a
    recurrence is reclassified off its memory destination (#2663): the root-KPI
    rule forbids re-promoting another memory for a recurrence, so it is sent to a
    teatree-core destination and Pass-2 triage tickets it as a core gap instead.

    Returns a :class:`WriteOutcome` tallying rows written vs rejected. Under *dry_run*
    the tally is computed but nothing is written.
    """
    from teatree.loops.dream.compliance import reclassify_recurring_memory_clusters  # noqa: PLC0415 — import cycle

    clusters = reclassify_recurring_memory_clusters(clusters)
    snippet_texts = {str(snippet.path): normalize_ws(snippet.text) for snippet in extract.snippets}
    snippet_weights = {str(snippet.path): snippet.weight for snippet in extract.snippets}
    written = 0
    rejected = 0
    for cluster in clusters:
        if not cluster_is_grounded(cluster, snippet_texts):
            rejected += 1
            logger.warning(
                "dream ledger REJECTED an ungrounded cluster (rule=%r, source_files=%s): "
                "its verified_citation is not present in a cited snippet — not recording it.",
                cluster.rule[:120],
                cluster.source_files,
            )
            continue
        written += 1
        if not dry_run:
            _upsert(cluster, max_member_weight=_cited_max_weight(cluster, snippet_weights), overlay=overlay)
    return WriteOutcome(written=written, rejected=rejected)


#: Unicode punctuation folded to its ASCII equivalent before the grounding
#: substring test. A decoded transcript may carry a smart quote / em-dash where the
#: model's citation used the straight form (or the reverse), so both operands are
#: folded SYMMETRICALLY (:func:`normalize_ws` runs on the snippet index AND on the
#: citation). This stays a strict substring test — a canonical form on both sides,
#: never a fuzzy / token-overlap match — so an invented citation is still rejected.
_PUNCT_FOLD = str.maketrans(
    {
        "\u2018": "'",  # left single quotation mark
        "\u2019": "'",  # right single quotation mark
        "\u201a": "'",  # single low-9 quotation mark
        "\u201b": "'",  # single high-reversed-9 quotation mark
        "\u201c": '"',  # left double quotation mark
        "\u201d": '"',  # right double quotation mark
        "\u201e": '"',  # double low-9 quotation mark
        "\u201f": '"',  # double high-reversed-9 quotation mark
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2015": "-",  # horizontal bar
        "\u2212": "-",  # minus sign
        "\u2026": "...",  # horizontal ellipsis
    }
)


def normalize_ws(text: str) -> str:
    return " ".join(text.translate(_PUNCT_FOLD).split())


def cluster_is_grounded(cluster: DistilledCluster, snippet_texts: dict[str, str]) -> bool:
    sources = [str(path) for path in cluster.source_files if str(path).strip()]
    if not sources or any(source not in snippet_texts for source in sources):
        return False
    citation = normalize_ws(cluster.verified_citation)
    if not citation:
        return False
    return any(citation in snippet_texts[source] for source in sources)


def _cited_max_weight(cluster: DistilledCluster, snippet_weights: dict[str, int]) -> int:
    weights = [snippet_weights[str(path)] for path in cluster.source_files if str(path) in snippet_weights]
    return max(weights, default=0)


def _upsert(cluster: DistilledCluster, *, max_member_weight: int, overlay: str) -> None:
    from teatree.core.models import ConsolidatedMemory  # noqa: PLC0415 — deferred: ORM import needs the app registry

    sources = [str(path) for path in cluster.source_files]
    row = ConsolidatedMemory.record_cluster(
        cluster_key=cluster.cluster_key,
        rule=cluster.rule,
        source_files=sources,
        member_count=len(sources),
        max_member_weight=max_member_weight,
        is_binding=cluster.is_binding,
        overlay=overlay,
        durable_destination=cluster.durable_destination,
    )
    # durable_destination is triage metadata, not the binding rule, so keep it
    # current on an existing row even when binding — BEFORE the binding early-out.
    if cluster.durable_destination and cluster.durable_destination != row.durable_destination:
        row.durable_destination = cluster.durable_destination
        row.save(update_fields=["durable_destination", "updated_at"])
    if row.is_binding:
        return
    row.rule = cluster.rule
    row.source_files = sources
    row.member_count = len(sources)
    row.max_member_weight = max_member_weight
    row.save(
        update_fields=["rule", "source_files", "member_count", "max_member_weight", "durable_destination", "updated_at"]
    )


def run_consolidation(
    *,
    overlay: str,
    since: datetime | None,
    dry_run: bool,
    distiller: Distiller | None = None,
    eval_proposals: "EvalProposalRequest | None" = None,
) -> DreamRunResult:
    """Run one consolidation pass: replay → extract → distill → write to ledger.

    *distiller* defaults to the real SDK distiller; tests inject a fake so the
    engine runs without an LLM. A distiller failure propagates to the caller
    (the command marks the pass attempted-not-succeeded), never swallowed.

    *eval_proposals* is OFF by default (``None``): an unflagged pass is
    byte-identical to before. When a request is supplied, the sibling
    :mod:`teatree.loops.dream.eval_proposer` derives inert eval candidates from the
    grounded clusters and appends them to the review queue — only candidate
    descriptors, never a scenario file or fixture.
    """
    from teatree.loops.dream.distill import (  # noqa: PLC0415 — deferred: import cycle
        commit_distill_cursor,
        distill_in_batches,
    )
    from teatree.loops.dream.sdk_distiller import sdk_distill  # noqa: PLC0415 — deferred: import cycle

    members = enumerate_members(since=since)
    extract = build_extract(members)
    distill = distiller or sdk_distill
    outcome = distill_in_batches(extract, distiller=distill, dry_run=dry_run)
    if outcome.deferred_members:
        logger.warning(
            "dream pass DEFERRED %d of %d snippet(s) — the per-pass batch cap bound; "
            "they are carried to the next pass by the distill cursor, not dropped.",
            outcome.deferred_members,
            len(extract.snippets),
        )
    clusters = outcome.clusters
    # ONE transaction for the rows and for the cursor that CLAIMS those rows exist. The
    # cursor means "this region of the corpus has been folded in"; committing it in a
    # separate autocommit BEFORE the write — which is what the distiller used to do —
    # let anything raising in between strand that claim against no rows at all, and the
    # rotation does not revisit a window it skipped until it has wrapped the whole corpus.
    with transaction.atomic():
        write_outcome = write_clusters(clusters, extract, dry_run=dry_run, overlay=overlay)
        if outcome.next_cursor is not None:
            commit_distill_cursor(outcome.next_cursor)
    proposed = 0
    if eval_proposals is not None:
        from teatree.loops.dream import eval_proposer  # noqa: PLC0415 — deferred: loaded at tick time, not import

        proposals = eval_proposer.propose_evals(clusters, extract, proposer=eval_proposals.proposer)
        proposed = eval_proposer.write_eval_proposals(proposals, dry_run=dry_run, out_path=eval_proposals.out_path)
    return DreamRunResult(
        clusters_recorded=write_outcome.written,
        members_replayed=len(members),
        dry_run=dry_run,
        evals_proposed=proposed,
        empty_batches=outcome.empty_batches,
        snippets_distilled=outcome.snippets_distilled,
        clusters_rejected=write_outcome.rejected,
        failed_batches=outcome.failed_batches,
        broken_batches=outcome.broken_batches,
        distill_diagnostics=outcome.diagnostics,
        deferred_members=outcome.deferred_members,
        extract=extract,
    )


__all__ = [
    "ConsolidationExtract",
    "DistillEmptyReason",
    "DistillResult",
    "DistilledCluster",
    "Distiller",
    "DreamRunResult",
    "TranscriptMember",
    "WeightedSnippet",
    "WriteOutcome",
    "build_extract",
    "cluster_is_grounded",
    "default_projects_dir",
    "enumerate_members",
    "looks_like_user_correction",
    "normalize_ws",
    "run_consolidation",
    "write_clusters",
]
