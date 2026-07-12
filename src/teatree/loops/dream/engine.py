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
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Protocol

from teatree.loops.dream.transcript_extract import high_signal_lines, looks_like_user_correction

if TYPE_CHECKING:
    from teatree.loops.dream.eval_proposer import EvalProposalRequest

logger = logging.getLogger(__name__)

_DEFAULT_LOOKBACK_HOURS = 48

#: Weight floors per member, highest signal first. The ladder is KIND-AWARE
#: (:func:`_member_weight`): the ``BINDING`` / ``feedback_`` doctrine floors are
#: reserved for CURATED MEMORY files, so a transcript that merely QUOTES a BINDING
#: rule can never outrank the memory that owns it. A fresh user-correction turn in a
#: transcript carries its own high floor (``_WEIGHT_CORRECTION``, just under feedback)
#: — the day's highest-signal drift. Retro / cold-review / deny-streak markers rank
#: below, then anything else — so the bounded extract keeps the highest-signal
#: members when it truncates.
_WEIGHT_BINDING = 100
_WEIGHT_FEEDBACK = 90
_WEIGHT_CORRECTION = 80
_WEIGHT_RETRO = 70
_WEIGHT_COLD_REVIEW = 50
_WEIGHT_DENY_STREAK = 40
_WEIGHT_OTHER = 10

#: Per-memory text cap; combines with the extract ceiling to bound the prompt. A
#: curated memory file is dense doctrine, so a tight cap keeps any single memory
#: from crowding the prompt.
_PER_SNIPPET_CHARS = 4000

#: Per-transcript-session text cap on the high-signal lines kept from ONE session,
#: so a single flooding session (a giant task output) can never dominate the extract
#: at the expense of the rest of the corpus.
_PER_SESSION_CHARS = 8_000


@dataclass(frozen=True, slots=True)
class TranscriptMember:
    path: Path
    kind: str


@dataclass(frozen=True, slots=True)
class WeightedSnippet:
    path: Path
    kind: str
    weight: int
    text: str


@dataclass(frozen=True, slots=True)
class ConsolidationExtract:
    """The bounded, ranked input one dream pass feeds the distiller."""

    CHAR_CEILING: ClassVar[int] = 60_000

    #: A guaranteed slice of the ceiling reserved for CURATED MEMORY members, filled
    #: FIRST so a flood of recent transcript members (a night of large task outputs)
    #: can never starve the durable doctrine out of the prompt. Complements
    #: :data:`TRANSCRIPT_FLOOR`: the two floors protect the prompt from EITHER side
    #: flooding the other, and the remainder is filled highest-weight-first.
    MEMORY_FLOOR: ClassVar[int] = 16_000

    #: A guaranteed slice of the ceiling reserved for recent transcript members,
    #: filled after the memory floor so high-weight curated-memory re-reads can never
    #: starve fresh drift out of the prompt. The remainder is filled highest-weight-first.
    TRANSCRIPT_FLOOR: ClassVar[int] = 24_000

    snippets: tuple[WeightedSnippet, ...]
    truncated: bool


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


@dataclass(frozen=True, slots=True)
class DistillResult:
    """A distiller call's clusters plus, when empty, WHY (#2847).

    ``empty_reason`` is set exactly when ``clusters`` is empty and ``None`` otherwise,
    so :func:`distill_in_batches` can surface a broken parse distinctly from a genuine
    no-consolidation in its 0-cluster WARNING.
    """

    clusters: list[DistilledCluster]
    empty_reason: DistillEmptyReason | None


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
    #: The bounded extract this pass built, so the command can reuse it for the
    #: compliance-measurement and automatable-ask phases instead of re-enumerating +
    #: re-reading every member a second time. ``None`` only on a pass that built none.
    extract: "ConsolidationExtract | None" = None


def default_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _task_output_roots() -> list[Path]:
    uid = os.geteuid()
    candidate = Path(f"/tmp/claude-{uid}")  # noqa: S108 — fixed agent-controlled path, not user input
    return [candidate] if candidate.is_dir() else []


def _is_recent_file(path: Path, cutoff_ts: float) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    return st.st_mtime >= cutoff_ts


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except OSError:
        return False


def enumerate_members(
    *,
    since: datetime | None = None,
    lookback_hours: int = _DEFAULT_LOOKBACK_HOURS,
    projects_dir: Path | None = None,
    task_output_roots: list[Path] | None = None,
) -> list[TranscriptMember]:
    if since is not None:
        cutoff = since if since.tzinfo is not None else since.replace(tzinfo=UTC)
    else:
        cutoff = datetime.now(tz=UTC) - timedelta(hours=lookback_hours)

    cutoff_ts = cutoff.timestamp()
    root = projects_dir or default_projects_dir()
    task_roots = task_output_roots if task_output_roots is not None else _task_output_roots()

    members: list[TranscriptMember] = []

    if root.is_dir():
        members.extend(
            TranscriptMember(path=p, kind="memory") for p in root.glob("*/memory/*.md") if _is_regular_file(p)
        )
        members.extend(
            TranscriptMember(path=p, kind="main") for p in root.glob("*/*.jsonl") if _is_recent_file(p, cutoff_ts)
        )
        members.extend(
            TranscriptMember(path=p, kind="subagent")
            for p in root.glob("*/*/subagents/agent-*.jsonl")
            if _is_recent_file(p, cutoff_ts)
        )

    for task_root in task_roots:
        members.extend(
            TranscriptMember(path=p, kind="task_output")
            for p in task_root.glob("*/*/tasks/*.output")
            if _is_recent_file(p, cutoff_ts)
        )

    members.sort(key=lambda m: m.path.stat().st_mtime, reverse=True)
    return members


def _member_weight(member: TranscriptMember, text: str) -> int:
    """Rank a member by KIND-AWARE signal so a transcript never impersonates doctrine.

    The ``BINDING`` / ``feedback_`` doctrine floors are reserved for CURATED MEMORY
    members: a session/task transcript that merely QUOTES a BINDING rule is drift
    ABOUT the rule, not the rule itself, so it must not tie or outrank the memory that
    owns it. A transcript's own high floor is a fresh USER-CORRECTION turn
    (``_WEIGHT_CORRECTION``) — the day's richest drift. Retro / cold-review /
    deny-streak markers apply to either kind; everything else is baseline.
    """
    name = member.path.name.lower()
    body = text.lower()
    if member.kind == "memory":
        if "binding" in body:
            return _WEIGHT_BINDING
        if name.startswith("feedback_"):
            return _WEIGHT_FEEDBACK
    elif _has_user_correction(text):
        return _WEIGHT_CORRECTION
    return _shared_marker_weight(name, body)


def _shared_marker_weight(name: str, body: str) -> int:
    """The kind-agnostic tail of the weight ladder shared by memory and transcript members."""
    if "retro" in name or "retro finding" in body:
        return _WEIGHT_RETRO
    if "cold review" in body or "cold-review" in name:
        return _WEIGHT_COLD_REVIEW
    if "denied" in body or "deny-streak" in body:
        return _WEIGHT_DENY_STREAK
    return _WEIGHT_OTHER


def _has_user_correction(text: str) -> bool:
    """True when any line of *text* reads like a raw user-correction turn.

    Reuses :func:`looks_like_user_correction` (the keyword-blind ground-truth signal)
    per line so a transcript carrying a fresh correction earns the correction floor.
    """
    return any(looks_like_user_correction(line) for line in text.splitlines())


def _read_member_text(member: TranscriptMember) -> str:
    try:
        raw = member.path.read_text(errors="replace")
    except OSError:
        return ""
    if member.kind == "memory" or member.path.suffix == ".md":
        return raw[:_PER_SNIPPET_CHARS]
    return high_signal_lines(raw)[:_PER_SESSION_CHARS]


def _is_transcript(snippet: WeightedSnippet) -> bool:
    return snippet.kind != "memory"


def build_extract(members: Sequence[TranscriptMember]) -> ConsolidationExtract:
    """Read, rank, and hard-bound the members into a distiller input.

    Each member is read once; transcript members keep only high-signal lines
    (gate BLOCKs, user-corrections, retro markers) so raw chatter never reaches
    the LLM. TWO guaranteed floors protect the prompt from either side flooding the
    other: a ``MEMORY_FLOOR`` slice is filled FIRST from curated-memory members so a
    night of large task outputs can never starve durable doctrine out of the prompt,
    then a ``TRANSCRIPT_FLOOR`` slice is filled from recent transcript members so a
    flood of high-weight memory re-reads can never starve fresh drift. The remaining
    budget is then filled highest-weight-first over everything not already kept.
    Snippets are ordered by a WEIGHT-ONLY stable sort, so equal-weight members keep
    their input (recency) order. ``truncated`` flips when a member is clipped or
    dropped for lack of budget.
    """
    weighted: list[WeightedSnippet] = []
    for member in members:
        text = _read_member_text(member)
        if not text.strip():
            continue
        weighted.append(
            WeightedSnippet(path=member.path, kind=member.kind, weight=_member_weight(member, text), text=text),
        )
    weighted.sort(key=lambda s: s.weight, reverse=True)

    kept: list[WeightedSnippet] = []
    seen: set[int] = set()
    used = 0

    memories = [s for s in weighted if not _is_transcript(s)]
    transcripts = [s for s in weighted if _is_transcript(s)]
    used, mem_truncated = _fill(memories, kept, seen, used, ceiling=ConsolidationExtract.MEMORY_FLOOR)
    used, transcript_truncated = _fill(
        transcripts, kept, seen, used, ceiling=ConsolidationExtract.MEMORY_FLOOR + ConsolidationExtract.TRANSCRIPT_FLOOR
    )
    used, rest_truncated = _fill(weighted, kept, seen, used, ceiling=ConsolidationExtract.CHAR_CEILING)

    return ConsolidationExtract(snippets=tuple(kept), truncated=mem_truncated or transcript_truncated or rest_truncated)


def _fill(
    candidates: Sequence[WeightedSnippet],
    kept: list[WeightedSnippet],
    seen: set[int],
    used: int,
    *,
    ceiling: int,
) -> tuple[int, bool]:
    truncated = False
    for snippet in candidates:
        if id(snippet) in seen:
            continue
        if used + len(snippet.text) > ceiling:
            remaining = ceiling - used
            if remaining > 0:
                kept.append(_clip(snippet, remaining))
                seen.add(id(snippet))
                used += remaining
            truncated = True
            break
        kept.append(snippet)
        seen.add(id(snippet))
        used += len(snippet.text)
    return used, truncated


def _clip(snippet: WeightedSnippet, length: int) -> WeightedSnippet:
    return WeightedSnippet(path=snippet.path, kind=snippet.kind, weight=snippet.weight, text=snippet.text[:length])


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


def normalize_ws(text: str) -> str:
    return " ".join(text.split())


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
    from teatree.loops.dream.distill import distill_in_batches  # noqa: PLC0415 — deferred: import cycle
    from teatree.loops.dream.sdk_distiller import sdk_distill  # noqa: PLC0415 — deferred: import cycle

    members = enumerate_members(since=since)
    extract = build_extract(members)
    distill = distiller or sdk_distill
    outcome = distill_in_batches(extract, distiller=distill)
    clusters = outcome.clusters
    write_outcome = write_clusters(clusters, extract, dry_run=dry_run, overlay=overlay)
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
        snippets_distilled=len(extract.snippets),
        clusters_rejected=write_outcome.rejected,
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
