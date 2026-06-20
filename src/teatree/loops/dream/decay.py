"""Phase 6 of the dream pass — decay / archive stale memories (#1933 § 6, § 2).

The memory set grows monotonically; without decay it accumulates stale lessons
that drown the live ones. This phase ages out a memory by ARCHIVING it (moving it
to ``<memory_dir>/archive/`` with a provenance header recording why and when) —
NEVER a blind delete, so an archived lesson is always recoverable.

The retention guard is the load-bearing part and is NON-VACUOUS by construction.
A memory is RETAINED (never archived) when ANY of:

*   it was written recently (mtime within the retention window), OR
*   it is still REFERENCED — another live memory ``[[name]]``-links it, or the
    ``MEMORY.md`` index still lists it, OR
*   its lesson has NO confirmed durable home in the ``ConsolidatedMemory``
    ledger — the **transfer-before-prune rail** (#1933 § 2, #2546). The §2 rail
    is *"delete an index line only after the fact has a confirmed durable home in
    a topic file"*; decay applies the same safety to the topic file itself, so a
    memory is never aged out until its lesson has been demonstrably transferred.

Only a memory that is old AND unreferenced AND has a confirmed durable home is
archived. The anti-vacuity test proves every direction: a fresh memory is
skipped, a linked memory is skipped, a stale + unreferenced + *un-homed* memory
is RETAINED, and a stale + unreferenced + *homed* one is archived — and flipping
each guard off would archive the protected memory, so each guard demonstrably has
teeth.

The durable-home check is an injected :data:`HomeResolver` seam so the file-side
mechanics stay pure and DB-free under test; the production default is
:func:`ledger_durable_home_resolver`, which reads
:meth:`teatree.core.models.ConsolidatedMemory.objects.prunable` (a terminal status
+ a recorded ``durable_destination``) once per pass and maps a topic file to a
ledger row by path membership in ``source_files`` OR by its name appearing in a
``durable_destination``.

PURE w.r.t. the real ``~/.claude``: the caller passes an explicit ``memory_dir``
and a ``now``/``retention`` policy; tests pass a tmp fixture and a fixed clock.
Fault-isolated: the command runs it in a try/except so a phase-6 failure never
crashes the tick.
"""

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

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


#: The transfer-before-prune seam: given a memory file, has its lesson been
#: demonstrably transferred to a confirmed durable home? Injected so the
#: file-side mechanics stay DB-free under test; the production default is
#: :func:`ledger_durable_home_resolver`.
HomeResolver = Callable[[_MemoryFile], bool]


def ledger_durable_home_resolver() -> HomeResolver:
    """Build the production durable-home resolver from the ``ConsolidatedMemory`` ledger.

    Reads :meth:`ConsolidatedMemory.objects.prunable` ONCE (terminal status +
    recorded ``durable_destination`` — the same surface the index pruner uses for
    *"transfer before prune"*) and returns a predicate that is True for a memory
    file iff a prunable row HOMES it: the memory's path is a member of the row's
    ``source_files`` (its lesson was transferred elsewhere), or the memory's name
    appears in the row's ``durable_destination`` (the rule was promoted INTO it).
    A non-terminal / un-promoted row has no durable destination and so homes
    nothing, which is exactly the rail — a memory with no confirmed home is never
    aged out.
    """
    from teatree.core.models import ConsolidatedMemory  # noqa: PLC0415

    rows = list(ConsolidatedMemory.objects.prunable())
    homed_source_paths: set[str] = set()
    destinations: list[str] = []
    for row in rows:
        homed_source_paths.update(_source_path_strings(row.source_files))
        if row.durable_destination:
            destinations.append(row.durable_destination)

    def _has_home(memory: _MemoryFile) -> bool:
        if str(memory.path) in homed_source_paths:
            return True
        targets = {memory.path.name, memory.name}
        return any(target and target in destination for destination in destinations for target in targets)

    return _has_home


def _source_path_strings(source_files: object) -> set[str]:
    """Normalize a ledger row's ``source_files`` JSON into the set of member path strings.

    A member is stored either as a bare path string or as a ``{"path": ...}``
    object (the engine writes bare strings; older/manual rows may carry the
    object form). Anything else is ignored.
    """
    if not isinstance(source_files, list):
        return set()
    paths: set[str] = set()
    for member in source_files:
        if isinstance(member, str):
            paths.add(member)
        elif isinstance(member, Mapping):
            path = cast("Mapping[str, object]", member).get("path")
            if isinstance(path, str):
                paths.add(path)
    return paths


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
    files: Sequence[_MemoryFile],
    index_text: str,
    now: datetime,
    retention: timedelta,
    has_durable_home: HomeResolver,
) -> Iterable[_MemoryFile]:
    """Yield only the memories that are old AND unreferenced AND durably homed — the guard.

    A fresh memory (mtime within *retention*) is retained; a referenced memory is
    retained; and — the transfer-before-prune rail (#2546) — a memory whose lesson
    has NO confirmed durable home is retained even when old + unreferenced. Only a
    memory failing all three tests is a decay candidate.
    """
    cutoff = now - retention
    for memory in files:
        if memory.mtime >= cutoff:
            continue  # fresh — retained
        if _is_referenced(memory, files, index_text):
            continue  # referenced — retained
        if not has_durable_home(memory):
            continue  # no confirmed durable home — retained (transfer before prune)
        yield memory


def decay_memories(
    memory_dir: Path,
    *,
    now: datetime | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    dry_run: bool = False,
    has_durable_home: HomeResolver | None = None,
) -> DecayResult:
    """Archive memories that are stale AND unreferenced AND durably homed; retain the rest.

    A fresh (recently-written) or referenced/linked memory is never archived — the
    non-vacuous retention guard. The transfer-before-prune rail adds a third
    retention reason: a memory whose lesson has no confirmed durable home in the
    ``ConsolidatedMemory`` ledger is retained even when old + unreferenced, so a
    lesson is never aged out before it has been transferred (#1933 § 2, #2546). An
    archived memory is MOVED to ``<memory_dir>/archive/`` with a provenance header,
    never deleted. A missing dir is a clean no-op. Under *dry_run* the decision is
    computed but nothing moves.

    *has_durable_home* is the injected resolver seam; when ``None`` the production
    :func:`ledger_durable_home_resolver` is built (reads the ledger once).
    """
    moment = now or datetime.now(tz=UTC)
    if not memory_dir.is_dir():
        return DecayResult(seen=0, archived=(), retained=0, dry_run=dry_run)
    resolver = has_durable_home if has_durable_home is not None else ledger_durable_home_resolver()
    files = _load_memory_files(memory_dir)
    index_path = memory_dir / _INDEX_NAME
    index_text = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
    retention = timedelta(days=retention_days)
    archive_dir = memory_dir / _ARCHIVE_DIRNAME

    archived: list[ArchivedMemory] = [
        _archive_one(memory, archive_dir, moment, reason="stale, unreferenced, durably homed", dry_run=dry_run)
        for memory in _stale_candidates(files, index_text, moment, retention, resolver)
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
    "HomeResolver",
    "decay_memories",
    "ledger_durable_home_resolver",
]
