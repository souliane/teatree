"""Phase 6 of the dream pass — decay / archive stale memories (#1933 § 6, § 2).

The memory set grows monotonically; without decay it accumulates stale lessons
that drown the live ones. This phase ages out a memory by ARCHIVING it (moving it
to ``<memory_dir>/archive/`` with a provenance header recording why and when) —
NEVER a blind delete, so an archived lesson is always recoverable.

The retention guard is the load-bearing part and is NON-VACUOUS by construction.
A memory is RETAINED (never archived) when ANY of:

*   it was written recently (mtime within the retention window), OR
*   it is still REFERENCED — another live memory cites it, or the ``MEMORY.md``
    index still lists it. A citation is resolved by canonicalizing every alias UP
    to the memory's FILENAME (:func:`_canonical_by_alias`), because a memory
    declares a hyphenated, prefix-stripped frontmatter ``name`` while its citers
    write the filename form; comparing the two forms directly resolves to zero and
    reads a heavily-cited rule as orphaned. Both ``[[wikilink]]`` and ``name.md``
    citations count (markdown link target, backticked, or bare in a curated grouped
    index line); the lone ``- name.md`` pointer re-index writes for every file does
    not, since it regenerates wholesale and can never dangle, OR
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

The SECOND tier — the BUDGET tier (#2723) — exists because the curated corpus has
~294 must-preserve (user / BINDING) entries, whose rendered index can exceed the
hot-index session-load budgets (~24 KB of bytes, 200 lines), and the ledger home-rail is
structurally empty for hand-authored memories (it can never archive them). When the hot
``MEMORY.md`` is over EITHER budget the tier scores every file by :func:`_signal_score`
(user / BINDING / inbound links / recency / type) and archives the LOWEST-signal first —
only as many as it takes to bring the projected hot index back under BOTH — so the
highest-signal entries that fit stay HOT and the rest move to a COLD tier: ``archive/``
holds the full restorable body and the cold
``MEMORY_ARCHIVE.md`` index holds one signature line per archived entry. The cold index
lives in the main memory dir (so the gate snapshot still finds the signature — retention
stays green) but is NEVER re-indexed into the hot ``MEMORY.md``. Referenced entries are
NOT hard-retained by the budget tier (#2753): the cross-link phase runs before decay and
references most of the corpus, so a hard skip floored the tier above budget and it could
never converge. Instead ``_signal_score`` adds +40 per inbound citation, so referenced
entries rank HIGHER and are archived LAST — only when the budget genuinely forces it —
staying restorable in ``archive/`` and recall-able via the cold ``MEMORY_ARCHIVE.md``.
(The conservative stale/ledger tier keeps its reference skip; only the budget tier drops
it.) When the budget does force a cited entry out, its citers are recorded on
:attr:`ArchivedMemory.broken_inbound` and the caller warns — a load-bearing memory can be
archived under pressure, but never SILENTLY.

PURE w.r.t. the real ``~/.claude``: the caller passes an explicit ``memory_dir``
and a ``now``/``retention`` policy; tests pass a tmp fixture and a fixed clock.
Fault-isolated: the command runs it in a try/except so a phase-6 failure never
crashes the tick.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from teatree.loops.dream._shared import ARCHIVE_INDEX_NAME, INDEX_NAME
from teatree.loops.dream.decay_corpus import MemoryFile, inbound_citers, is_referenced, load_memory_files
from teatree.loops.dream.decay_signal import BudgetProjection, budget_tier_candidates

#: Default retention window — a memory written within this many days is kept
#: regardless of references (a fresh lesson is never stale). Generous on purpose.
DEFAULT_RETENTION_DAYS = 30

ARCHIVE_DIRNAME = "archive"

#: The COLD archive index (#2723), written by this phase in the MAIN memory dir so the
#: gate snapshot globs it as a memory body (an archived entry's signature stays findable
#: there, keeping retention green) while it is NEVER re-indexed, cross-linked, or
#: itself archived/merged — excluded alongside ``MEMORY.md`` in every loader.


#: Preamble of the cold ``MEMORY_ARCHIVE.md`` — kept machine-readable (one
#: ``- <name>.md — <original signature>`` line per entry) for a future recall pass.
_COLD_HEADER = (
    "# Auto Memory — Cold Archive Index\n\n"
    "> Low-signal memories archived out of the hot MEMORY.md to keep it under the "
    "session-load budget. NOT loaded at session start; searchable here, full bodies "
    "in archive/ (restorable). One line per entry: `- <name>.md — <original signature>`.\n\n"
)


@dataclass(frozen=True, slots=True)
class BudgetTier:
    """The on-disk RETIRE tier marker (#2723) — opt in via :class:`DecayPolicy`.

    When supplied AND the hot ``MEMORY.md`` is over the load budget, decay archives the
    LOWEST-:func:`_signal_score` files first — just enough to bring the projected hot
    index back under budget. The tier needs no knobs of its own: the freshness window
    is :attr:`DecayPolicy.retention_days` and the budget is the gate-(d) constants.
    """


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
    """One memory the decay phase archived — its old path, new path, reason, and cost.

    ``broken_inbound`` names every live document that cited this memory and will now
    point at nothing. A non-empty tuple is what the caller renders as a warning, so a
    load-bearing memory can still be archived under budget pressure but never SILENTLY.
    """

    name: str
    source: Path
    destination: Path
    reason: str
    broken_inbound: tuple[str, ...] = ()


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


#: The transfer-before-prune seam: given a memory file, has its lesson been
#: demonstrably transferred to a confirmed durable home? Injected so the
#: file-side mechanics stay DB-free under test; the production default is
#: :func:`ledger_durable_home_resolver`.
HomeResolver = Callable[[MemoryFile], bool]


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
    from teatree.core.models import ConsolidatedMemory  # noqa: PLC0415 — deferred: ORM import needs the app registry

    rows = list(ConsolidatedMemory.objects.prunable())
    homed_source_paths: set[str] = set()
    destinations: list[str] = []
    for row in rows:
        homed_source_paths.update(_source_path_strings(row.source_files))
        if row.durable_destination:
            destinations.append(row.durable_destination)

    def _has_home(memory: MemoryFile) -> bool:
        if str(memory.path) in homed_source_paths:
            return True
        targets = {memory.path.name, memory.name}
        return any(target and target in destination for destination in destinations for target in targets)

    return _has_home


def cold_archive_names(archive_dir: Path | None) -> set[str]:
    """Memory filenames preserved in the durable ``archive/`` cold store.

    A file MOVED to ``archive/`` — this pass, a PRIOR pass, or absorbed by the merge
    phase — keeps its full body there and its signature in ``MEMORY_ARCHIVE.md``: a
    confirmed durable home, exactly the §2 transfer-before-prune destination (#2723).
    The §4 consolidation gate homes a pruned hot-index pointer at such a file against
    this set, so a stale pointer to an already-archived memory is not flagged a loss —
    unlike a pointer at a genuinely deleted memory, which has no cold-store entry. A
    ``None`` / missing dir is the empty set (no cold home is known).
    """
    if archive_dir is None or not archive_dir.is_dir():
        return set()
    return {md.name for md in archive_dir.glob("*.md")}


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


def _provenance_header(memory: MemoryFile, now: datetime, reason: str) -> str:
    return (
        f"<!-- archived by dream decay {now.date().isoformat()}: {reason}; "
        f"original mtime {memory.mtime.date().isoformat()} -->\n"
    )


def _unique_archive_destination(archive_dir: Path, filename: str) -> Path:
    """A non-colliding archive path for *filename* under *archive_dir*.

    Never blind-overwrites a prior archived lesson (the "never blind delete"
    invariant): on a name collision the destination gets a numeric suffix
    (``feedback_x.md`` → ``feedback_x.1.md`` → …) so an earlier archived body is
    preserved alongside the new one.
    """
    destination = archive_dir / filename
    if not destination.exists():
        return destination
    stem, suffix = Path(filename).stem, Path(filename).suffix
    counter = 1
    while (candidate := archive_dir / f"{stem}.{counter}{suffix}").exists():
        counter += 1
    return candidate


@dataclass(frozen=True, slots=True)
class _Archival:
    """Why a memory is being archived, and which live citations that will break."""

    reason: str
    breaks: tuple[str, ...] = ()


def _archive_one(
    memory: MemoryFile, archive_dir: Path, now: datetime, archival: _Archival, *, dry_run: bool
) -> ArchivedMemory:
    if dry_run:
        return ArchivedMemory(
            name=memory.name,
            source=memory.path,
            destination=archive_dir / memory.path.name,
            reason=archival.reason,
            broken_inbound=archival.breaks,
        )
    archive_dir.mkdir(parents=True, exist_ok=True)
    destination = _unique_archive_destination(archive_dir, memory.path.name)
    destination.write_text(_provenance_header(memory, now, archival.reason) + memory.text, encoding="utf-8")
    memory.path.unlink()
    return ArchivedMemory(
        name=memory.name,
        source=memory.path,
        destination=destination,
        reason=archival.reason,
        broken_inbound=archival.breaks,
    )


def _stale_candidates(
    files: Sequence[MemoryFile],
    citers: Mapping[str, tuple[str, ...]],
    now: datetime,
    retention: timedelta,
    has_durable_home: HomeResolver,
) -> Iterable[MemoryFile]:
    """Yield only the memories that are old AND unreferenced AND durably homed — the guard.

    A fresh memory (``lesson_touched`` within *retention*) is retained; a referenced
    memory is retained; and — the transfer-before-prune rail (#2546) — a memory whose
    lesson has NO confirmed durable home is retained even when old + unreferenced. Only
    a memory failing all three tests is a decay candidate.

    Ages by the LOGICAL ``lesson_touched`` clock, not raw ``st_mtime``: cross-link and
    re-index rewrite a file (bumping ``st_mtime``) without touching its lesson, so
    keying on ``st_mtime`` would keep a linked memory perpetually "fresh" and the
    transfer-before-prune tier would never fire. Same clock the budget tier uses.
    """
    cutoff = now - retention
    for memory in files:
        if memory.lesson_touched >= cutoff:
            continue  # fresh — retained
        if is_referenced(memory, citers):
            continue  # referenced — retained
        if not has_durable_home(memory):
            continue  # no confirmed durable home — retained (transfer before prune)
        yield memory


def _strip_provenance(text: str) -> str:
    """Drop the leading ``<!-- archived by dream decay ... -->`` provenance line.

    So the cold-index signature is computed from the ORIGINAL body (matching the
    retention probe, which lifts its signature from the pre-archival text).
    """
    if text.startswith("<!--"):
        _comment, marker, rest = text.partition("-->\n")
        if marker:
            return rest
    return text


def _cold_index_line(archived_md: Path) -> str:
    """One ``- <name>.md — <original signature>`` cold-index line for an archived file.

    The signature is computed from the original body (provenance header stripped) with
    the SAME helper the retention gate uses, so ``snapshot.contains(signature)`` is True
    for the archived entry — its lesson stays answerable from the cold index. Uncapped:
    the verbatim signature is what retention needs.
    """
    from teatree.loops.dream.gates import _signature_line  # noqa: PLC0415 — deferred: loaded at tick time, not import

    try:
        text = archived_md.read_text(encoding="utf-8")
    except OSError:
        return ""
    signature = _signature_line(_strip_provenance(text))
    return f"- {archived_md.name} — {signature}" if signature else f"- {archived_md.name}"


def _rebuild_cold_index(memory_dir: Path, archive_dir: Path) -> None:
    """Rebuild the cold ``MEMORY_ARCHIVE.md`` from EVERY file under ``archive/``.

    One line per archived entry, carrying its full unclipped original signature. Rebuilt
    wholesale each pass (idempotent) so the cold tier accumulates across passes and a
    second pass rewrites it byte-identically. Written in the MAIN memory dir so the gate
    snapshot globs it as a memory body, keeping the retention / interference gates green
    for archived entries; it is excluded from every re-index / cross-link / decay loader,
    so it never re-bloats the hot index. A no-op when nothing has been archived.
    """
    if not archive_dir.is_dir():
        return
    lines = [line for md in sorted(archive_dir.glob("*.md")) if (line := _cold_index_line(md))]
    if not lines:
        return
    (memory_dir / ARCHIVE_INDEX_NAME).write_text(_COLD_HEADER + "\n".join(lines) + "\n", encoding="utf-8")


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
    ``prunable()`` join can never reach: when the hot ``MEMORY.md`` is over the load
    budget, decay ALSO archives the LOWEST-:func:`_signal_score` files first — just
    enough to bring the projected hot index back under budget. The default policy (no
    budget tier) leaves the ledger-home tier alone — byte-identical to before.

    Whichever tier fires, the cold ``MEMORY_ARCHIVE.md`` is rebuilt from ``archive/`` so
    every archived entry's signature stays findable (retention-safe) while its full body
    remains in ``archive/`` (restorable).
    """
    settings = policy or DecayPolicy()
    moment = now or datetime.now(tz=UTC)
    if not memory_dir.is_dir():
        return DecayResult(seen=0, archived=(), retained=0, dry_run=dry_run)
    resolver = has_durable_home if has_durable_home is not None else ledger_durable_home_resolver()
    files = load_memory_files(memory_dir)
    index_path = memory_dir / INDEX_NAME
    index_text = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
    retention = timedelta(days=settings.retention_days)
    archive_dir = memory_dir / ARCHIVE_DIRNAME

    citers = inbound_citers(files, index_text)

    home_tier = list(_stale_candidates(files, citers, moment, retention, resolver))
    archived: list[ArchivedMemory] = [
        _archive_one(memory, archive_dir, moment, _Archival("stale, unreferenced, durably homed"), dry_run=dry_run)
        for memory in home_tier
    ]
    if settings.budget_tier is not None:
        homed_paths = {m.path for m in home_tier}
        remaining = [m for m in files if m.path not in homed_paths]
        projection = BudgetProjection(
            memory_dir=memory_dir, index_text=index_text, citers=citers, now=moment, retention=retention
        )
        archived.extend(
            _archive_one(
                memory,
                archive_dir,
                moment,
                _Archival("over-budget, lowest-signal", citers.get(memory.path.name, ())),
                dry_run=dry_run,
            )
            for memory in budget_tier_candidates(remaining, projection)
        )
    if not dry_run:
        _rebuild_cold_index(memory_dir, archive_dir)
    return DecayResult(
        seen=len(files),
        archived=tuple(archived),
        retained=len(files) - len(archived),
        dry_run=dry_run,
    )


__all__ = [
    "ARCHIVE_DIRNAME",
    "DEFAULT_RETENTION_DAYS",
    "ArchivedMemory",
    "BudgetTier",
    "DecayPolicy",
    "DecayResult",
    "HomeResolver",
    "cold_archive_names",
    "decay_memories",
    "ledger_durable_home_resolver",
]
