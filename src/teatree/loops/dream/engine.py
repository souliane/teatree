"""Distillation-engine SEAM for the idle-time dream pass (#1933).

This module is the single, well-named entry point the dream cron exercises so
the whole orchestration around it — the in-flight lease, the ``--dry-run``
no-write path, ``DreamRunMarker`` stamping, and the staleness alarm — is fully
testable WITHOUT an LLM.

TODO(#1933): implement the distillation phases. Per the issue's § 2 phase plan,
adapted from arXiv:2606.03979 (schedule + safety ordering only — no weight
updates):

1. Replay — re-read raw source memory files + recent session transcripts
    (retro findings, user-correction memories highest-weight, cold reviews,
    deny-streaks, …), not summaries-of-summaries.
2. Dedup / merge / cluster — group entries sharing one root cause.
3. Distill — one imperative rule per cluster; an LLM rewrite of markdown,
    never a verbatim copy of episodes. Verify-before-durable-write: a rule is
    written only if it would have prevented a real, CITED mistake.
4. (Optional) Cross-link — low-temperature pass surfacing latent shared root
    causes across unrelated entries.
5. Re-index — rewrite ``MEMORY.md`` so each surviving cluster has a <=1-line
    entry, bringing the index back under its load budget.
6. Decay / archive — remove a volatile index line ONLY after the fact has a
    confirmed durable home (transfer-before-prune); archive, never hard-delete;
    BINDING feedback is never silently dropped.

The output store is the DB-backed ``ConsolidatedMemory`` ledger.

Deferred (do NOT decide here — see issue #1933 § 6 open questions):
- The QA-probe corpus source feeding the retention/interference/monotonicity
    gates (``DreamQaProbe``) and whether/how it is persisted across runs.
- The exact archive location and the restore-on-recurrence trigger.
- Whether the phase-4 cross-link pass ships in v1 or is deferred.
"""

import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

_DEFAULT_LOOKBACK_HOURS = 48


@dataclass(frozen=True, slots=True)
class TranscriptMember:
    path: Path
    kind: str


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


def run_consolidation(*, overlay: str, since: datetime | None, dry_run: bool) -> DreamRunResult:
    del overlay
    members = enumerate_members(since=since)
    return DreamRunResult(clusters_recorded=0, members_replayed=len(members), dry_run=dry_run)
