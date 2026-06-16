"""Phase 6 of the dream pass — decay / archive stale memories (#1933 § 6).

The memory set grows monotonically; without decay it accumulates stale lessons
that drown the live ones. This phase ages out a memory by ARCHIVING it (moving it
to ``<memory_dir>/archive/`` with a provenance header recording why and when) —
NEVER a blind delete, so an archived lesson is always recoverable.

The retention guard is the load-bearing part and is NON-VACUOUS by construction:
a memory is RETAINED (never archived) when EITHER

*   it was written recently (mtime within the retention window), OR
*   it is still REFERENCED — another live memory ``[[name]]``-links it, or the
    ``MEMORY.md`` index still lists it.

Only a memory that is BOTH old AND unreferenced is archived. The anti-vacuity
test proves both directions: a fresh memory is skipped, a linked memory is
skipped, and a genuinely-stale unreferenced one is archived — and flipping the
guard off (archiving regardless) would archive the fresh/linked memory, so the
guard demonstrably has teeth.

PURE w.r.t. the real ``~/.claude``: the caller passes an explicit ``memory_dir``
and a ``now``/``retention`` policy; tests pass a tmp fixture and a fixed clock.
Fault-isolated: the command runs it in a try/except so a phase-6 failure never
crashes the tick.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: Default retention window — a memory written within this many days is kept
#: regardless of references (a fresh lesson is never stale). Generous on purpose.
DEFAULT_RETENTION_DAYS = 30

_ARCHIVE_DIRNAME = "archive"
_INDEX_NAME = "MEMORY.md"
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_FRONTMATTER_NAME_RE = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class ArchivedMemory:
    """One memory the decay phase archived — its old path, new path, and reason."""

    name: str
    source: Path
    destination: Path
    reason: str


@dataclass(frozen=True, slots=True)
class DecayResult:
    """What one phase-6 pass did: candidates seen, archived, retained, whether dry."""

    seen: int
    archived: tuple[ArchivedMemory, ...]
    retained: int
    dry_run: bool

    @property
    def archived_count(self) -> int:
        return len(self.archived)


@dataclass(frozen=True, slots=True)
class _MemoryFile:
    path: Path
    name: str
    text: str
    mtime: datetime


def _memory_name(path: Path, text: str) -> str:
    match = _FRONTMATTER_NAME_RE.search(text)
    return match.group(1) if match else path.stem


def _load_memory_files(memory_dir: Path) -> list[_MemoryFile]:
    files: list[_MemoryFile] = []
    for md in sorted(memory_dir.glob("*.md")):
        if md.name == _INDEX_NAME:
            continue
        try:
            text = md.read_text(encoding="utf-8")
            mtime = datetime.fromtimestamp(md.stat().st_mtime, tz=UTC)
        except OSError:
            continue
        files.append(_MemoryFile(path=md, name=_memory_name(md, text), text=text, mtime=mtime))
    return files


def _is_referenced(memory: _MemoryFile, files: Sequence[_MemoryFile], index_text: str) -> bool:
    """True iff a memory OTHER than *memory* (or the index) links *memory*'s name."""
    if memory.name in _WIKILINK_RE.findall(index_text):
        return True
    return any(other.path != memory.path and memory.name in _WIKILINK_RE.findall(other.text) for other in files)


def _provenance_header(memory: _MemoryFile, now: datetime, reason: str) -> str:
    return (
        f"<!-- archived by dream decay {now.date().isoformat()}: {reason}; "
        f"original mtime {memory.mtime.date().isoformat()} -->\n"
    )


def _archive_one(
    memory: _MemoryFile, archive_dir: Path, now: datetime, reason: str, *, dry_run: bool
) -> ArchivedMemory:
    destination = archive_dir / memory.path.name
    if not dry_run:
        archive_dir.mkdir(parents=True, exist_ok=True)
        destination.write_text(_provenance_header(memory, now, reason) + memory.text, encoding="utf-8")
        memory.path.unlink()
    return ArchivedMemory(name=memory.name, source=memory.path, destination=destination, reason=reason)


def _stale_candidates(
    files: Sequence[_MemoryFile], index_text: str, now: datetime, retention: timedelta
) -> Iterable[_MemoryFile]:
    """Yield only the memories that are BOTH old AND unreferenced — the guard.

    A fresh memory (mtime within *retention*) is retained; a referenced memory is
    retained; only a memory failing BOTH tests is a decay candidate.
    """
    cutoff = now - retention
    for memory in files:
        if memory.mtime >= cutoff:
            continue  # fresh — retained
        if _is_referenced(memory, files, index_text):
            continue  # referenced — retained
        yield memory


def decay_memories(
    memory_dir: Path,
    *,
    now: datetime | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    dry_run: bool = False,
) -> DecayResult:
    """Archive memories that are BOTH stale AND unreferenced; retain the rest.

    A fresh (recently-written) or referenced/linked memory is never archived — the
    non-vacuous retention guard. An archived memory is MOVED to
    ``<memory_dir>/archive/`` with a provenance header, never deleted. A missing
    dir is a clean no-op. Under *dry_run* the decision is computed but nothing
    moves.
    """
    moment = now or datetime.now(tz=UTC)
    if not memory_dir.is_dir():
        return DecayResult(seen=0, archived=(), retained=0, dry_run=dry_run)
    files = _load_memory_files(memory_dir)
    index_path = memory_dir / _INDEX_NAME
    index_text = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
    retention = timedelta(days=retention_days)
    archive_dir = memory_dir / _ARCHIVE_DIRNAME

    archived: list[ArchivedMemory] = [
        _archive_one(memory, archive_dir, moment, reason="stale and unreferenced", dry_run=dry_run)
        for memory in _stale_candidates(files, index_text, moment, retention)
    ]
    return DecayResult(
        seen=len(files),
        archived=tuple(archived),
        retained=len(files) - len(archived),
        dry_run=dry_run,
    )


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "ArchivedMemory",
    "DecayResult",
    "decay_memories",
]
