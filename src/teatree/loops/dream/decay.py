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

#: Hard age ceiling for the BUDGET decay tier (#2723, Decision-2). When the index
#: exceeds the load budget, a file older than this AND unreferenced AND whose
#: lesson is captured by a near-duplicate survivor is archivable — INDEPENDENT of
#: the (structurally-empty for the curated corpus) ledger home-rail.
BUDGET_AGE_CEILING_DAYS = 90

_ARCHIVE_DIRNAME = "archive"
_INDEX_NAME = "MEMORY.md"
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_FRONTMATTER_NAME_RE = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)
#: A logical "lesson last-touched" frontmatter date — the age clock the budget tier
#: reads so a cross-link / re-index rewrite (which bumps ``st_mtime``) does NOT reset
#: the decay clock. Absent the field, the budget tier falls back to ``st_mtime``.
_LESSON_UPDATED_RE = re.compile(r"^lesson_updated:\s*(\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class BudgetTier:
    """The on-disk RETIRE tier config (#2723) — opt in via :class:`DecayPolicy`.

    Bundles the budget tier's knobs. When supplied AND the index is over the load
    budget, decay archives files older than ``age_ceiling_days`` (by the logical
    lesson clock) that are unreferenced and whose lesson a near-duplicate survivor
    captures.
    """

    age_ceiling_days: int = BUDGET_AGE_CEILING_DAYS


@dataclass(frozen=True, slots=True)
class DecayPolicy:
    """The decay tuning knobs — the freshness window and the optional budget tier.

    Bundles the two policy dimensions so the ``decay_memories`` entry point stays
    narrow (the execution context — clock, dry-run, home resolver — stays as
    explicit kwargs). ``budget_tier`` is ``None`` by default (ledger-home tier only,
    byte-identical to before).
    """

    retention_days: int = DEFAULT_RETENTION_DAYS
    budget_tier: BudgetTier | None = None


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

    @property
    def lesson_touched(self) -> datetime:
        """The logical lesson last-touched time — frontmatter ``lesson_updated`` or mtime.

        The budget tier ages a lesson by WHEN IT WAS LAST MEANINGFULLY UPDATED, not
        when the file was last written: cross-link and re-index rewrite a file (and
        bump ``st_mtime``) without touching the lesson, so keying the decay clock on
        ``st_mtime`` would keep resetting it. The ``lesson_updated`` frontmatter date
        is that logical clock; absent it, ``st_mtime`` is the conservative fallback.
        """
        match = _LESSON_UPDATED_RE.search(self.text)
        if match:
            try:
                parsed = datetime.fromisoformat(match.group(1))
            except ValueError:
                return self.mtime
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        return self.mtime


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


def _index_over_budget(index_text: str) -> bool:
    """Whether the rendered ``MEMORY.md`` exceeds the gate-(d) session-load budget.

    Reuses the §4 budget constants so the decay-pressure trigger and the gate that
    grades the result agree on what "over budget" means (#2723).
    """
    from teatree.loops.dream.gates import INDEX_BYTE_BUDGET, INDEX_LINE_BUDGET  # noqa: PLC0415

    line_count = sum(1 for line in index_text.splitlines() if line.strip())
    return len(index_text.encode("utf-8")) > INDEX_BYTE_BUDGET or line_count > INDEX_LINE_BUDGET


def _captured_elsewhere(memory: _MemoryFile, others: Sequence[_MemoryFile]) -> bool:
    """Whether *memory*'s lesson is captured by a near-duplicate SURVIVOR.

    The budget tier's safety rail: a file is only archivable when another file
    (which is NOT itself being archived this pass) records the same lesson — a
    body-token near-duplicate above the merge floor. A genuinely UNIQUE lesson with
    no twin is retained, even over budget. This is the content/duplication check the
    plan mandates in place of the structurally-empty ``prunable()`` ledger join.
    """
    from teatree.loops.dream.cross_link import _jaccard  # noqa: PLC0415
    from teatree.loops.dream.merge import _NEAR_DUPLICATE_FLOOR, _body_tokens  # noqa: PLC0415

    mine = _body_tokens(memory)
    if not mine:
        return False
    return any(
        other.path != memory.path and _jaccard(mine, _body_tokens(other)) >= _NEAR_DUPLICATE_FLOOR for other in others
    )


def _budget_tier_candidates(
    files: Sequence[_MemoryFile], index_text: str, now: datetime, ceiling: timedelta
) -> Iterable[_MemoryFile]:
    """Yield budget-tier archival candidates: old AND unreferenced AND captured elsewhere.

    Fires ONLY when the index is over budget. A candidate's lesson age reads the
    logical ``lesson_touched`` clock (not ``st_mtime``), it must be unreferenced, and
    its lesson must be captured by a near-duplicate survivor — never a unique lesson.
    Candidates are removed from the survivor pool greedily so two twins do not both
    archive each other away (one always survives).
    """
    if not _index_over_budget(index_text):
        return
    cutoff = now - ceiling
    survivors = list(files)
    for memory in sorted(files, key=lambda m: m.lesson_touched):
        if memory.lesson_touched >= cutoff:
            continue  # lesson recently touched — retained
        if _is_referenced(memory, files, index_text):
            continue  # referenced — retained
        remaining = [m for m in survivors if m.path != memory.path]
        if not _captured_elsewhere(memory, remaining):
            continue  # unique lesson with no twin — retained (captured-elsewhere rail)
        survivors = remaining
        yield memory


def decay_memories(
    memory_dir: Path,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    has_durable_home: HomeResolver | None = None,
    policy: DecayPolicy | None = None,
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

    *policy* bundles the freshness window and the optional budget tier. A
    :class:`DecayPolicy` with a :class:`BudgetTier` opts into a SECOND,
    ledger-INDEPENDENT decay tier (#2723) for the hand-authored corpus the empty
    ``prunable()`` join can never reach: when the index is over the load budget,
    decay ALSO archives files older than the tier's ``age_ceiling_days`` (by the
    logical ``lesson_touched`` clock) that are unreferenced AND whose lesson a
    near-duplicate survivor captures. The default policy (no budget tier) leaves the
    ledger-home tier alone — byte-identical to before.
    """
    settings = policy or DecayPolicy()
    moment = now or datetime.now(tz=UTC)
    if not memory_dir.is_dir():
        return DecayResult(seen=0, archived=(), retained=0, dry_run=dry_run)
    resolver = has_durable_home if has_durable_home is not None else ledger_durable_home_resolver()
    files = _load_memory_files(memory_dir)
    index_path = memory_dir / _INDEX_NAME
    index_text = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
    retention = timedelta(days=settings.retention_days)
    archive_dir = memory_dir / _ARCHIVE_DIRNAME

    home_tier = list(_stale_candidates(files, index_text, moment, retention, resolver))
    archived: list[ArchivedMemory] = [
        _archive_one(memory, archive_dir, moment, reason="stale, unreferenced, durably homed", dry_run=dry_run)
        for memory in home_tier
    ]
    if settings.budget_tier is not None:
        homed_paths = {m.path for m in home_tier}
        remaining = [m for m in files if m.path not in homed_paths]
        ceiling = timedelta(days=settings.budget_tier.age_ceiling_days)
        archived.extend(
            _archive_one(
                memory,
                archive_dir,
                moment,
                reason="over-budget, stale, unreferenced, captured elsewhere",
                dry_run=dry_run,
            )
            for memory in _budget_tier_candidates(remaining, index_text, moment, ceiling)
        )
    return DecayResult(
        seen=len(files),
        archived=tuple(archived),
        retained=len(files) - len(archived),
        dry_run=dry_run,
    )


__all__ = [
    "BUDGET_AGE_CEILING_DAYS",
    "DEFAULT_RETENTION_DAYS",
    "ArchivedMemory",
    "BudgetTier",
    "DecayPolicy",
    "DecayResult",
    "HomeResolver",
    "decay_memories",
    "ledger_durable_home_resolver",
]
