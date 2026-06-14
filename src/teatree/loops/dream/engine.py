"""Distillation engine for the idle-time dream pass (#1933).

This module is the single, well-named entry point the dream cron exercises so
the whole orchestration around it — the in-flight lease, the ``--dry-run``
no-write path, ``DreamRunMarker`` stamping, and the staleness alarm — is fully
testable WITHOUT an LLM.

The pass runs in three phases, adapted from arXiv:2606.03979 (schedule + safety
ordering only — no weight updates):

Phase 1 (replay / extract) — :func:`enumerate_members` lists recent memory
files and session transcripts; :func:`build_extract` reads them, ranks by weight
(user-correction BINDING/``feedback_*`` highest, then retro findings, cold
reviews, deny-streaks, other), keeps only high-signal transcript lines, and
bounds the total size hard so the LLM prompt can never blow up.

Phase 2 (cluster / distill) — the injected :class:`Distiller` groups the extract
by root cause and returns one imperative :class:`DistilledCluster` per group,
each citing a real mistake present in the extract. The real distiller
(:func:`_sdk_distiller`) makes ONE bounded headless ``claude-agent-sdk`` call
(the headless-runner invocation shape) and parses its JSON defensively; tests
inject a fake so the engine needs no LLM.

Phase 3 (write to the ledger) — :func:`write_clusters` rejects any uncited /
unknown-source / blank-citation cluster (no hallucinated memories), idempotently
upserts by ``cluster_key`` via the manager (no duplicate rows on re-run), and
never destructively overwrites a BINDING row's rule.

The output store is the DB-backed :class:`~teatree.core.models.ConsolidatedMemory`
ledger; :func:`write_clusters` reuses its ``record_cluster`` factory rather than
bypassing the manager.

Deferred (out of scope for v1 — see issue #1933 § 6): the optional phase-4
cross-link pass; the phase-5 ``MEMORY.md`` re-index (rewriting the user's real
155KB index with BINDING entries); the phase-6 decay / archive of the memory
``.md`` files; and the QA-probe gates (``DreamQaProbe``
retention/interference/monotonicity). All four are high-file-blast-radius
follow-ups — never an autonomous v1 file rewrite — and v1 leaves ``DreamQaProbe``
behaviour untouched.
"""

import json
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar, Protocol, cast

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

#: Transcript lines worth keeping — the rest is chatter that must never reach
#: the LLM prompt (the extract is a distillation input, not a raw replay).
_TRANSCRIPT_SIGNALS = (
    "TEATREE GATE",
    "BLOCK",
    "DENIED",
    "feedback_",
    "BINDING",
    "retro",
    "user-correction",
    "cold review",
)

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


def _projects_dir() -> Path:
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
    root = projects_dir or _projects_dir()
    task_roots = task_output_roots if task_output_roots is not None else _task_output_roots()

    members: list[TranscriptMember] = []

    if root.is_dir():
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
    return _high_signal_lines(raw)[:_PER_SNIPPET_CHARS]


def _high_signal_lines(raw: str) -> str:
    kept = [line for line in raw.splitlines() if any(signal in line for signal in _TRANSCRIPT_SIGNALS)]
    return "\n".join(kept)


def build_extract(members: Sequence[TranscriptMember]) -> ConsolidationExtract:
    """Read, rank, and hard-bound the members into a distiller input.

    Each member is read once; transcript members keep only high-signal lines
    (gate BLOCKs, user-corrections, retro markers) so raw chatter never reaches
    the LLM. Snippets are ranked highest-weight first and accumulated until the
    overall char ceiling is reached — at which point ``truncated`` flips and the
    remaining lower-weight members are dropped.
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
    used = 0
    truncated = False
    for snippet in weighted:
        if used + len(snippet.text) > ConsolidationExtract.CHAR_CEILING:
            remaining = ConsolidationExtract.CHAR_CEILING - used
            if remaining > 0:
                kept.append(_clip(snippet, remaining))
                used += remaining
            truncated = True
            break
        kept.append(snippet)
        used += len(snippet.text)

    return ConsolidationExtract(snippets=tuple(kept), truncated=truncated)


def _clip(snippet: WeightedSnippet, length: int) -> WeightedSnippet:
    return WeightedSnippet(path=snippet.path, kind=snippet.kind, weight=snippet.weight, text=snippet.text[:length])


def write_clusters(
    clusters: Sequence[DistilledCluster],
    members: Sequence[TranscriptMember],
    *,
    dry_run: bool,
    overlay: str = "",
) -> int:
    """Idempotently record valid clusters into the ConsolidatedMemory ledger.

    A cluster is rejected (counted as skipped, never written) when its
    ``source_files`` is empty, cites a path not in *members*, or its
    ``verified_citation`` is blank — these are the hallucinated-rule shapes the
    ledger must never persist. A valid cluster is upserted by ``cluster_key``
    through the manager factory, so a re-run that re-clusters the same members
    updates the row in place instead of duplicating it. A BINDING row's ``rule``
    is never destructively overwritten.

    Returns the count of clusters that passed the reject guard. Under *dry_run*
    the count is computed but nothing is written.
    """
    member_paths = {str(member.path) for member in members}
    written = 0
    for cluster in clusters:
        if not _cluster_is_grounded(cluster, member_paths):
            continue
        written += 1
        if not dry_run:
            _upsert(cluster, overlay=overlay)
    return written


def _cluster_is_grounded(cluster: DistilledCluster, member_paths: set[str]) -> bool:
    sources = [str(path) for path in cluster.source_files if str(path).strip()]
    if not sources or any(source not in member_paths for source in sources):
        return False
    return bool(cluster.verified_citation.strip())


def _upsert(cluster: DistilledCluster, *, overlay: str) -> None:
    from teatree.core.models import ConsolidatedMemory  # noqa: PLC0415

    sources = [str(path) for path in cluster.source_files]
    row = ConsolidatedMemory.record_cluster(
        cluster_key=cluster.cluster_key,
        rule=cluster.rule,
        source_files=sources,
        member_count=len(sources),
        max_member_weight=0,
        is_binding=cluster.is_binding,
        overlay=overlay,
    )
    if row.is_binding:
        return
    row.rule = cluster.rule
    row.source_files = sources
    row.member_count = len(sources)
    row.save(update_fields=["rule", "source_files", "member_count", "updated_at"])


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
    "verified_citation (the specific real mistake, quoted from a snippet, that "
    "the rule would have prevented), durable_destination (a suggested home).\n\n"
    "Emit an element ONLY when verified_citation quotes a real mistake present "
    "below. If nothing meets the bar, return [].\n\n"
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
) -> DreamRunResult:
    """Run one consolidation pass: replay → extract → distill → write to ledger.

    *distiller* defaults to the real SDK distiller; tests inject a fake so the
    engine runs without an LLM. A distiller failure propagates to the caller
    (the command marks the pass attempted-not-succeeded), never swallowed.
    """
    members = enumerate_members(since=since)
    extract = build_extract(members)
    distill = distiller or _sdk_distiller
    clusters = distill(extract)
    written = write_clusters(clusters, members, dry_run=dry_run, overlay=overlay)
    return DreamRunResult(clusters_recorded=written, members_replayed=len(members), dry_run=dry_run)


__all__ = [
    "ConsolidationExtract",
    "DistilledCluster",
    "Distiller",
    "DreamRunResult",
    "TranscriptMember",
    "WeightedSnippet",
    "build_extract",
    "enumerate_members",
    "run_consolidation",
    "write_clusters",
]
