"""Phase 1 of the dream pass — REPLAY the corpus and rank it into a bounded extract.

The half of the pipeline that decides WHAT the distiller is shown, split out of
:mod:`teatree.loops.dream.engine` (which keeps phases 2-3: distil, then write to the
ledger). Two jobs, and the vocabulary they produce:

*   :func:`enumerate_members` lists what exists — the curated memory files, re-read
    regardless of age, plus the recency-gated session / sub-agent / task-output
    transcripts.
*   :func:`build_extract` reads them, weights each by the shared ladder, keeps only the
    high-signal transcript lines, and truncates to a bounded
    :class:`ConsolidationExtract` in rank order.

The ranking is the load-bearing part, because the truncation is what makes the budget
survivable: whatever the ceiling cuts is the LOWEST-signal material, and the doctrine
and the freshest user corrections are never what is dropped. The weight ladder is
KIND-AWARE, so a transcript that merely QUOTES a BINDING rule can never outrank the
curated memory that owns it.

Nothing here touches the DB or an LLM, and every root is resolved from an explicit
argument or ``Path.home()``, so the whole phase is exercisable from a tmp fixture.
"""

import logging
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

from teatree.loops.dream._shared import WEIGHT_BINDING as _WEIGHT_BINDING
from teatree.loops.dream._shared import WEIGHT_COLD_REVIEW as _WEIGHT_COLD_REVIEW
from teatree.loops.dream._shared import WEIGHT_CORRECTION as _WEIGHT_CORRECTION
from teatree.loops.dream._shared import WEIGHT_DENY_STREAK as _WEIGHT_DENY_STREAK
from teatree.loops.dream._shared import WEIGHT_FEEDBACK as _WEIGHT_FEEDBACK
from teatree.loops.dream._shared import WEIGHT_OTHER as _WEIGHT_OTHER
from teatree.loops.dream._shared import WEIGHT_RETRO as _WEIGHT_RETRO
from teatree.loops.dream._shared import is_binding_text
from teatree.loops.dream.transcript_extract import high_signal_lines, looks_like_user_correction

logger = logging.getLogger(__name__)

_DEFAULT_LOOKBACK_HOURS = 48

# The member weight ladder (teatree.loops.dream._shared.WEIGHT_*, shared with the merge
# phase) is KIND-AWARE at _member_weight: the BINDING / feedback_ doctrine floors are
# reserved for CURATED MEMORY files, so a transcript that merely QUOTES a BINDING rule can
# never outrank the memory that owns it. A fresh user-correction turn in a transcript
# carries its own high floor (_WEIGHT_CORRECTION, just under feedback) — the day's
# highest-signal drift. Retro / cold-review / deny-streak markers rank below, then
# anything else — so the bounded extract keeps the highest-signal members when it truncates.

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
    #: The file's mtime captured ONCE at enumeration. Re-stat'ing the path in the
    #: sort key would crash the whole pass if a transcript (a /tmp session .jsonl)
    #: is reaped between enumeration and sort; capturing it up front is race-safe.
    mtime: float = 0.0


@dataclass(frozen=True, slots=True)
class WeightedSnippet:
    path: Path
    kind: str
    weight: int
    text: str


@dataclass(frozen=True, slots=True)
class ConsolidationExtract:
    """The bounded, ranked input one dream pass feeds the distiller."""

    #: The per-PROMPT character budget. It bounds ONE distiller call, never the
    #: corpus: :func:`~teatree.loops.dream.distill.distill_in_batches` partitions the
    #: ranked members into batches of at most this size, so a corpus many times the
    #: budget is distilled across several calls instead of clipped down to one.
    CHAR_CEILING: ClassVar[int] = 60_000

    #: A guaranteed slice of the FIRST batch reserved for CURATED MEMORY members,
    #: placed FIRST so a flood of recent transcript members (a night of large task
    #: outputs) can never push the durable doctrine out of the batch that is always
    #: distilled. Complements :data:`TRANSCRIPT_FLOOR`: the two floors keep either
    #: side from displacing the other at the head of the ranking.
    MEMORY_FLOOR: ClassVar[int] = 16_000

    #: A guaranteed slice of the first batch reserved for recent transcript members,
    #: placed after the memory floor so high-weight curated-memory re-reads cannot
    #: push fresh drift out of it. Everything else follows highest-weight-first.
    TRANSCRIPT_FLOOR: ClassVar[int] = 24_000

    snippets: tuple[WeightedSnippet, ...]


def default_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


#: Both roots task-output transcripts land under; the split is temporal residue (#3585).
#: Since #3641 pinned TMPDIR in every compose service's environment, all NEW output
#: lands under the disk-backed /var/tmp even for `docker exec`-started processes, so
#: scanning /tmp is now redundant belt-and-braces — retained only to still find any
#: pre-fix or non-container residual transcripts.
_TASK_OUTPUT_TMP_BASES: tuple[str, ...] = ("/tmp", "/var/tmp")  # noqa: S108 — fixed agent-controlled paths


def _task_output_roots() -> list[Path]:
    uid = os.geteuid()
    roots: dict[Path, Path] = {}
    for base in _TASK_OUTPUT_TMP_BASES:
        candidate = Path(base) / f"claude-{uid}"
        if candidate.is_dir():
            roots.setdefault(candidate.resolve(), candidate)
    return list(roots.values())


def _regular_file_mtime(path: Path) -> float | None:
    """The mtime of *path* if it is a regular file, else ``None`` (missing / not a file)."""
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime if stat.S_ISREG(st.st_mode) else None


def _recent_file_mtime(path: Path, cutoff_ts: float) -> float | None:
    """The mtime of *path* if it is a regular file at/after *cutoff_ts*, else ``None``."""
    mtime = _regular_file_mtime(path)
    return mtime if mtime is not None and mtime >= cutoff_ts else None


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
            TranscriptMember(path=p, kind="memory", mtime=mt)
            for p in root.glob("*/memory/*.md")
            if (mt := _regular_file_mtime(p)) is not None
        )
        members.extend(
            TranscriptMember(path=p, kind="main", mtime=mt)
            for p in root.glob("*/*.jsonl")
            if (mt := _recent_file_mtime(p, cutoff_ts)) is not None
        )
        members.extend(
            TranscriptMember(path=p, kind="subagent", mtime=mt)
            for p in root.glob("*/*/subagents/agent-*.jsonl")
            if (mt := _recent_file_mtime(p, cutoff_ts)) is not None
        )

    for task_root in task_roots:
        members.extend(
            TranscriptMember(path=p, kind="task_output", mtime=mt)
            for p in task_root.glob("*/*/tasks/*.output")
            if (mt := _recent_file_mtime(p, cutoff_ts)) is not None
        )

    members.sort(key=lambda m: m.mtime, reverse=True)
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
        if is_binding_text(text):
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
    """Read and RANK every member into the distiller input — discarding none.

    Each member is read once; transcript members keep only high-signal lines
    (gate BLOCKs, user-corrections, retro markers) so raw chatter never reaches
    the LLM, and :data:`_PER_SNIPPET_CHARS` / :data:`_PER_SESSION_CHARS` bound what
    any single member contributes. What this function does NOT do is bound the
    corpus: the prompt budget belongs to one distiller call, so applying it here
    discarded whatever did not fit the first prompt — on a real machine 20 of 419
    members, permanently, every pass. Batching
    (:func:`~teatree.loops.dream.distill.distill_in_batches`) owns that budget now,
    and every ranked member reaches a call.

    The ORDER is the whole contract this leaves behind. Members sort by a WEIGHT-ONLY
    stable sort (equal weights keep input/recency order), then the two floors lead:
    a ``MEMORY_FLOOR`` slice of curated memory first so a night of large task outputs
    cannot displace durable doctrine, then a ``TRANSCRIPT_FLOOR`` slice of transcripts
    so high-weight memory re-reads cannot displace fresh drift. Everything else
    follows highest-weight-first. Since the head of the ranking becomes the batch that
    is always distilled, that ordering is what guarantees doctrine and fresh drift are
    seen on every pass.
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

    ordered: list[WeightedSnippet] = []
    placed: set[int] = set()
    used = _lead_with(
        [s for s in weighted if not _is_transcript(s)], ordered, placed, 0, ceiling=ConsolidationExtract.MEMORY_FLOOR
    )
    _lead_with(
        [s for s in weighted if _is_transcript(s)],
        ordered,
        placed,
        used,
        ceiling=ConsolidationExtract.MEMORY_FLOOR + ConsolidationExtract.TRANSCRIPT_FLOOR,
    )
    ordered.extend(snippet for snippet in weighted if id(snippet) not in placed)
    return ConsolidationExtract(snippets=tuple(ordered))


def _lead_with(
    candidates: Sequence[WeightedSnippet],
    ordered: list[WeightedSnippet],
    placed: set[int],
    used: int,
    *,
    ceiling: int,
) -> int:
    """Move *candidates* to the head of *ordered* until *ceiling* chars are spoken for.

    Stops at the first candidate that would cross the ceiling rather than clipping it —
    an unplaced member is not dropped, it simply falls back to its weight-ranked
    position among the remainder.
    """
    for snippet in candidates:
        if id(snippet) in placed:
            continue
        if used + len(snippet.text) > ceiling:
            break
        ordered.append(snippet)
        placed.add(id(snippet))
        used += len(snippet.text)
    return used


__all__ = [
    "ConsolidationExtract",
    "TranscriptMember",
    "WeightedSnippet",
    "build_extract",
    "default_projects_dir",
    "enumerate_members",
    "looks_like_user_correction",
]
