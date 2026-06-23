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
than swallowed. The real distiller (:func:`_sdk_distiller`) makes ONE bounded
headless ``claude-agent-sdk`` call per batch (the headless-runner invocation
shape) and parses its JSON defensively; tests inject a fake so the engine needs
no LLM.

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

import json
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Protocol, cast

from teatree.loops.dream.transcript_extract import high_signal_lines, looks_like_user_correction

if TYPE_CHECKING:
    from teatree.loops.dream.eval_proposer import EvalProposalRequest

_DEFAULT_LOOKBACK_HOURS = 48

#: Weight floors per member, highest signal first. A memory file whose name
#: marks it a user-correction (``feedback_*``) or whose body carries BINDING
#: doctrine outranks a retro finding, which outranks a cold review, then a
#: deny-streak, then anything else — so the bounded extract keeps the
#: highest-signal members when it truncates.
_WEIGHT_BINDING = 100
_WEIGHT_FEEDBACK = 90
_WEIGHT_RETRO = 70
_WEIGHT_COLD_REVIEW = 50
_WEIGHT_DENY_STREAK = 40
_WEIGHT_OTHER = 10

#: Per-member text cap; combines with the extract ceiling to bound the prompt.
_PER_SNIPPET_CHARS = 4000


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

    #: A guaranteed slice of the ceiling reserved for recent transcript members,
    #: filled FIRST so high-weight curated-memory re-reads can never starve fresh
    #: drift out of the prompt. The remainder is filled highest-weight-first.
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


class Distiller(Protocol):
    """The injected LLM seam: extract → root-cause clusters."""

    def __call__(self, extract: ConsolidationExtract) -> list[DistilledCluster]: ...


@dataclass(frozen=True, slots=True)
class DreamRunResult:
    clusters_recorded: int
    members_replayed: int
    dry_run: bool
    evals_proposed: int = 0
    empty_batches: int = 0


def default_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _task_output_roots() -> list[Path]:
    uid = os.geteuid()
    candidate = Path(f"/tmp/claude-{uid}")  # noqa: S108
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
    name = member.path.name.lower()
    body = text.lower()
    if "binding" in body:
        return _WEIGHT_BINDING
    if name.startswith("feedback_"):
        return _WEIGHT_FEEDBACK
    if "retro" in name or "retro finding" in body:
        return _WEIGHT_RETRO
    if "cold review" in body or "cold-review" in name:
        return _WEIGHT_COLD_REVIEW
    if "denied" in body or "deny-streak" in body:
        return _WEIGHT_DENY_STREAK
    return _WEIGHT_OTHER


def _read_member_text(member: TranscriptMember) -> str:
    try:
        raw = member.path.read_text(errors="replace")
    except OSError:
        return ""
    if member.kind == "memory" or member.path.suffix == ".md":
        return raw[:_PER_SNIPPET_CHARS]
    return high_signal_lines(raw)[:_PER_SNIPPET_CHARS]


def _is_transcript(snippet: WeightedSnippet) -> bool:
    return snippet.kind != "memory"


def build_extract(members: Sequence[TranscriptMember]) -> ConsolidationExtract:
    """Read, rank, and hard-bound the members into a distiller input.

    Each member is read once; transcript members keep only high-signal lines
    (gate BLOCKs, user-corrections, retro markers) so raw chatter never reaches
    the LLM. A guaranteed ``TRANSCRIPT_FLOOR`` slice of the ceiling is filled
    FIRST from recent transcript members (highest-weight first among them) so a
    flood of high-weight curated-memory re-reads can never starve fresh drift out
    of the prompt; the remaining budget is then filled highest-weight-first over
    everything not already kept. ``truncated`` flips when a member is clipped or
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
    weighted.sort(key=lambda s: (s.weight, str(s.path)), reverse=True)

    kept: list[WeightedSnippet] = []
    seen: set[int] = set()
    used = 0
    truncated = False

    transcripts = [s for s in weighted if _is_transcript(s)]
    used, truncated = _fill(transcripts, kept, seen, used, ceiling=ConsolidationExtract.TRANSCRIPT_FLOOR)
    used, rest_truncated = _fill(weighted, kept, seen, used, ceiling=ConsolidationExtract.CHAR_CEILING)

    return ConsolidationExtract(snippets=tuple(kept), truncated=truncated or rest_truncated)


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
) -> int:
    """Idempotently record valid clusters into the ConsolidatedMemory ledger.

    A cluster is rejected (counted as skipped, never written) when its
    ``source_files`` is empty, cites a path not present in *extract*, or its
    ``verified_citation`` does not appear (whitespace-normalized substring) in a
    cited snippet's text — these are the hallucinated-rule shapes the ledger must
    never persist, including a real-path-but-invented-quote citation. A valid
    cluster is upserted by ``cluster_key`` through the manager factory, so a
    re-run that re-clusters the same members updates the row in place instead of
    duplicating it. A BINDING row's ``rule`` is never destructively overwritten.

    Returns the count of clusters that passed the reject guard. Under *dry_run*
    the count is computed but nothing is written.
    """
    snippet_texts = {str(snippet.path): normalize_ws(snippet.text) for snippet in extract.snippets}
    snippet_weights = {str(snippet.path): snippet.weight for snippet in extract.snippets}
    written = 0
    for cluster in clusters:
        if not cluster_is_grounded(cluster, snippet_texts):
            continue
        written += 1
        if not dry_run:
            _upsert(cluster, max_member_weight=_cited_max_weight(cluster, snippet_weights), overlay=overlay)
    return written


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
    from teatree.core.models import ConsolidatedMemory  # noqa: PLC0415

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


_DISTILL_SYSTEM_PROMPT = (
    "You consolidate an agent's recent feedback and lessons into durable rules. "
    "Group the snippets by ROOT CAUSE. Emit ONE imperative rule per group, and a "
    "group ONLY when it cites a SPECIFIC real mistake quoted from the snippets — "
    "never invent a rule with no cited mistake."
)

_DISTILL_PROMPT_TEMPLATE = (
    "Consolidate the following weighted snippets into root-cause clusters.\n\n"
    "Return ONLY a JSON array. Each element is an object with keys: "
    "cluster_key (a stable lowercase slug), rule (one imperative sentence), "
    "source_files (the snippet paths the rule cites — copy them verbatim), "
    "is_binding (true when a source is a BINDING/user-correction), "
    "verified_citation (a VERBATIM substring copied from one of the cited "
    "snippets — the specific real mistake the rule would have prevented; do NOT "
    "paraphrase, the quote must appear word-for-word in the snippet), "
    "durable_destination (a suggested home).\n\n"
    "Emit an element ONLY when verified_citation is a real quote present in a "
    "cited snippet below. If nothing meets the bar, return [].\n\n"
    "Snippets:\n{snippets}"
)

_DISTILL_WATCHDOG_SECONDS = 5 * 60
_DISTILL_MODEL = "claude-haiku-4-5"
_REQUIRED_CLUSTER_KEYS = ("cluster_key", "rule", "source_files", "is_binding", "verified_citation")


def _sdk_distiller(extract: ConsolidationExtract) -> list[DistilledCluster]:
    """The real distiller: one bounded headless SDK call, parsed defensively.

    An empty extract short-circuits without an LLM call. Otherwise one bounded
    turn through :func:`_run_distiller_turn` produces JSON, which is parsed into
    clusters; malformed or partial JSON yields no clusters rather than a crash.
    An SDK failure propagates so the command marks the pass attempted-not-
    succeeded (staleness keeps firing) — never laundered into a fake success.
    """
    if not extract.snippets:
        return []
    raw = _run_distiller_turn(extract)
    return _parse_clusters(raw)


def _render_snippets(extract: ConsolidationExtract) -> str:
    return "\n\n".join(
        f"--- {snippet.path} (weight={snippet.weight}) ---\n{snippet.text}" for snippet in extract.snippets
    )


def _run_distiller_turn(extract: ConsolidationExtract) -> str:
    """Run one bounded headless ``claude-agent-sdk`` turn, returning its text.

    Reuses the headless-runner invocation shape (the ``claude_code`` preset,
    ``bypassPermissions``, a wall-clock watchdog via :func:`asyncio.wait_for`)
    for a single no-tool turn — the extract is already bounded, so the model
    only transforms text to JSON. Raises when ``claude`` is unavailable or the
    turn fails, so the caller never reports a fake success.
    """
    import asyncio  # noqa: PLC0415
    import shutil  # noqa: PLC0415

    if shutil.which("claude") is None:
        msg = "claude is not installed — the dream distiller cannot run"
        raise RuntimeError(msg)
    prompt = _DISTILL_PROMPT_TEMPLATE.format(snippets=_render_snippets(extract))
    return asyncio.run(_collect_turn(prompt))


async def _collect_turn(prompt: str) -> str:
    import asyncio  # noqa: PLC0415

    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, TextBlock  # noqa: PLC0415
    from claude_agent_sdk.types import SystemPromptPreset  # noqa: PLC0415

    options = ClaudeAgentOptions(
        system_prompt=SystemPromptPreset(type="preset", preset="claude_code", append=_DISTILL_SYSTEM_PROMPT),
        model=_DISTILL_MODEL,
        permission_mode="bypassPermissions",
        max_turns=1,
        allowed_tools=[],
    )
    parts: list[str] = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)

        async def _drain() -> list[object]:
            return [message async for message in client.receive_response()]

        for message in await asyncio.wait_for(_drain(), timeout=_DISTILL_WATCHDOG_SECONDS):
            if isinstance(message, AssistantMessage):
                parts.extend(block.text for block in message.content if isinstance(block, TextBlock))
    return "\n".join(parts)


def _parse_clusters(raw: str) -> list[DistilledCluster]:
    """Parse the distiller's JSON array into clusters, dropping malformed entries.

    Tolerates surrounding prose by scanning for the first ``[`` … matching
    ``]``. An entry missing a required key is skipped (not fatal), so one bad
    element never discards a whole valid batch.
    """
    payload = _extract_json_array(raw)
    if payload is None:
        return []
    clusters: list[DistilledCluster] = []
    for entry in payload:
        cluster = _coerce_cluster(entry)
        if cluster is not None:
            clusters.append(cluster)
    return clusters


def _extract_json_array(raw: str) -> list[object] | None:
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def _coerce_cluster(entry: object) -> DistilledCluster | None:
    if not isinstance(entry, Mapping):
        return None
    fields = cast("Mapping[str, object]", entry)
    if any(key not in fields for key in _REQUIRED_CLUSTER_KEYS):
        return None
    source_files = fields["source_files"]
    if not isinstance(source_files, list):
        return None
    return DistilledCluster(
        cluster_key=str(fields["cluster_key"]),
        rule=str(fields["rule"]),
        source_files=[str(path) for path in source_files],
        is_binding=bool(fields["is_binding"]),
        verified_citation=str(fields["verified_citation"]),
        durable_destination=str(fields.get("durable_destination", "")),
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
    from teatree.loops.dream.distill import distill_in_batches  # noqa: PLC0415

    members = enumerate_members(since=since)
    extract = build_extract(members)
    distill = distiller or _sdk_distiller
    outcome = distill_in_batches(extract, distiller=distill)
    clusters = outcome.clusters
    written = write_clusters(clusters, extract, dry_run=dry_run, overlay=overlay)
    proposed = 0
    if eval_proposals is not None:
        from teatree.loops.dream import eval_proposer  # noqa: PLC0415

        proposals = eval_proposer.propose_evals(clusters, extract, proposer=eval_proposals.proposer)
        proposed = eval_proposer.write_eval_proposals(proposals, dry_run=dry_run, out_path=eval_proposals.out_path)
    return DreamRunResult(
        clusters_recorded=written,
        members_replayed=len(members),
        dry_run=dry_run,
        evals_proposed=proposed,
        empty_batches=outcome.empty_batches,
    )


__all__ = [
    "ConsolidationExtract",
    "DistilledCluster",
    "Distiller",
    "DreamRunResult",
    "TranscriptMember",
    "WeightedSnippet",
    "build_extract",
    "cluster_is_grounded",
    "default_projects_dir",
    "enumerate_members",
    "looks_like_user_correction",
    "normalize_ws",
    "run_consolidation",
    "write_clusters",
]
