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

The SECOND tier — the BUDGET tier (#2723) — exists because the curated corpus has
~294 must-preserve (user / BINDING) entries, whose rendered index can exceed the ~24 KB
hot-index session-load BYTE budget, and the ledger home-rail is structurally empty for
hand-authored memories (it can never archive them). When the hot ``MEMORY.md`` is over
budget the budget tier scores every file by :func:`_signal_score` (user / BINDING /
inbound links / recency / type) and archives the LOWEST-signal first — only as many as it
takes to bring the projected hot index back under the BYTE budget — so the highest-signal
entries that fit ~24 KB stay HOT and the rest move to a COLD tier: ``archive/`` holds the
full restorable body and the cold
``MEMORY_ARCHIVE.md`` index holds one signature line per archived entry. The cold index
lives in the main memory dir (so the gate snapshot still finds the signature — retention
stays green) but is NEVER re-indexed into the hot ``MEMORY.md``. Referenced entries are
NOT hard-retained by the budget tier (#2753): the cross-link phase runs before decay and
references most of the corpus, so a hard skip floored the tier above budget and it could
never converge. Instead ``_signal_score`` adds +40 per inbound ``[[name]]`` link, so
referenced entries rank HIGHER and are archived LAST — only when the budget genuinely
forces it — staying restorable in ``archive/`` and recall-able via the cold
``MEMORY_ARCHIVE.md``. (The conservative stale/ledger tier keeps its reference skip; only
the budget tier drops it.)

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

ARCHIVE_DIRNAME = "archive"
_INDEX_NAME = "MEMORY.md"
#: The COLD archive index (#2723), written by this phase in the MAIN memory dir so the
#: gate snapshot globs it as a memory body (an archived entry's signature stays findable
#: there, keeping retention green) while it is NEVER re-indexed, cross-linked, or
#: itself archived/merged — excluded alongside ``MEMORY.md`` in every loader.
_ARCHIVE_INDEX_NAME = "MEMORY_ARCHIVE.md"
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_FRONTMATTER_NAME_RE = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)
#: A logical "lesson last-touched" frontmatter date — the age clock the budget tier
#: reads so a cross-link / re-index rewrite (which bumps ``st_mtime``) does NOT reset
#: the decay clock. Absent the field, the budget tier falls back to ``st_mtime``.
_LESSON_UPDATED_RE = re.compile(r"^lesson_updated:\s*(\S+)\s*$", re.MULTILINE)
#: Frontmatter ``type:`` (top-level or nested under ``metadata:``) — the memory's
#: declared kind, used for the type-weight signal. ``node_type:`` never matches.
_TYPE_LINE_RE = re.compile(r"^\s*type:\s*(\S+)\s*$", re.MULTILINE)

#: The recognised memory types (filename prefix or frontmatter ``metadata.type``).
_KNOWN_TYPES = frozenset({"user", "feedback", "retro", "reference", "project"})

#: Additive signal weights for :func:`_signal_score` — higher means keep HOT. ``user``
#: and BINDING dominate so they are archived only if the budget forces it; inbound
#: ``[[name]]`` wikilinks and recency add the rest; a per-type floor breaks ties.
_SIGNAL_USER = 1000
_SIGNAL_BINDING = 500
_SIGNAL_PER_INBOUND_LINK = 40
_SIGNAL_RECENT = 200
_TYPE_WEIGHTS = {"feedback": 90, "retro": 70, "reference": 30, "project": 20, "user": 10, "other": 10}

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
    from teatree.core.models import ConsolidatedMemory  # noqa: PLC0415 — deferred: ORM import needs the app registry

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


def _memory_name(path: Path, text: str) -> str:
    match = _FRONTMATTER_NAME_RE.search(text)
    return match.group(1) if match else path.stem


def _load_memory_files(memory_dir: Path) -> list[_MemoryFile]:
    files: list[_MemoryFile] = []
    for md in sorted(memory_dir.glob("*.md")):
        if md.name in {_INDEX_NAME, _ARCHIVE_INDEX_NAME}:  # never load an index as a memory (#2723)
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


def _archive_one(
    memory: _MemoryFile, archive_dir: Path, now: datetime, reason: str, *, dry_run: bool
) -> ArchivedMemory:
    if dry_run:
        return ArchivedMemory(
            name=memory.name, source=memory.path, destination=archive_dir / memory.path.name, reason=reason
        )
    archive_dir.mkdir(parents=True, exist_ok=True)
    destination = _unique_archive_destination(archive_dir, memory.path.name)
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
        if _is_referenced(memory, files, index_text):
            continue  # referenced — retained
        if not has_durable_home(memory):
            continue  # no confirmed durable home — retained (transfer before prune)
        yield memory


def _over_budget(byte_size: int) -> bool:
    """Whether an index of *byte_size* bytes is over the gate-(d) BYTE budget.

    The one place the §4 gate-(d) byte budget is compared, so the decay-pressure
    trigger and the gate that grades the result agree on "over budget" (#2723). Bytes
    are the only constraint — the harness truncates ``MEMORY.md`` by BYTES at session
    load, so line count is irrelevant to what reaches the agent (#2755).
    """
    from teatree.loops.dream.gates import INDEX_BYTE_BUDGET  # noqa: PLC0415 — deferred: loaded at tick time, not import

    return byte_size > INDEX_BYTE_BUDGET


def _index_over_budget(index_text: str) -> bool:
    """Whether the rendered ``MEMORY.md`` exceeds the gate-(d) session-load byte budget."""
    return _over_budget(len(index_text.encode("utf-8")))


def _resolved_type(memory: _MemoryFile) -> str:
    """The memory's type for the type-weight signal.

    Frontmatter ``metadata.type`` when present and recognised, else the filename prefix
    (``feedback_x`` -> ``feedback``), else ``other``. The ~96 older files with no
    parseable ``metadata.type`` fall back to the prefix; deterministic and DB-free.
    """
    match = _TYPE_LINE_RE.search(memory.text)
    if match:
        candidate = match.group(1).strip().lower()
        if candidate in _KNOWN_TYPES:
            return candidate
    prefix = memory.path.stem.split("_", 1)[0].lower()
    return prefix if prefix in _KNOWN_TYPES else "other"


def _is_user_memory(memory: _MemoryFile) -> bool:
    """True for a user-authored memory — frontmatter ``metadata.type: user`` OR a ``user_*`` filename."""
    return _resolved_type(memory) == "user" or memory.path.name.lower().startswith("user_")


def _is_binding_text(text: str) -> bool:
    """True when the memory carries BINDING / Non-Negotiable doctrine (mirrors the engine weight)."""
    lowered = text.lower()
    return "binding" in lowered or "non-negotiable" in lowered


def _inbound_link_counts(files: Sequence[_MemoryFile], index_text: str) -> dict[str, int]:
    """Map each memory NAME to the number of distinct documents that ``[[name]]``-link it.

    Counted in ONE pass (the index counts as one source, each other memory as one) so
    the per-file inbound-wikilink signal is O(N), not an O(N²) rescan per memory.
    """
    counts: dict[str, int] = {}
    for name in set(_WIKILINK_RE.findall(index_text)):
        counts[name] = counts.get(name, 0) + 1
    for source in files:
        for name in set(_WIKILINK_RE.findall(source.text)):
            if name == source.name:
                continue  # a memory linking itself is not an inbound reference
            counts[name] = counts.get(name, 0) + 1
    return counts


def _recency_score(memory: _MemoryFile, now: datetime, retention: timedelta) -> int:
    """Recency signal — +200 within the retention window, decaying linearly past it.

    Floored at 0. Reads the logical ``lesson_touched`` clock so a cross-link / re-index
    rewrite (which bumps ``st_mtime``) does not reset recency.
    """
    age = now - memory.lesson_touched
    if age <= retention:
        return _SIGNAL_RECENT
    return max(0, _SIGNAL_RECENT - (age - retention).days)


def _signal_score(memory: _MemoryFile, *, inbound_links: int, now: datetime, retention: timedelta) -> int:
    """The keep-HOT signal of a memory — higher means more worth keeping in ``MEMORY.md``.

    Composed ADDITIVELY (never short-circuits) from the signals that mark a lesson
    load-bearing: a user-authored memory (+1000), BINDING / Non-Negotiable doctrine
    (+500), each inbound ``[[name]]`` wikilink (+40, *inbound_links* precomputed by the
    caller via :func:`_inbound_link_counts` so scoring the whole set stays O(N)), recency
    by the logical ``lesson_touched`` clock (+200 within *retention*, decaying linearly
    with age beyond it), and a per-type floor (feedback 90 / retro 70 / reference 30 /
    project 20 / other 10). The budget tier archives LOWEST score first, so the
    highest-signal memories stay hot and user / BINDING entries are archived only if the
    budget forces it. DB-free and deterministic — usable under ``SimpleTestCase``.
    """
    score = _SIGNAL_USER if _is_user_memory(memory) else 0
    if _is_binding_text(memory.text):
        score += _SIGNAL_BINDING
    score += _SIGNAL_PER_INBOUND_LINK * inbound_links
    score += _recency_score(memory, now, retention)
    score += _TYPE_WEIGHTS.get(_resolved_type(memory), _TYPE_WEIGHTS["other"])
    return score


def _budget_tier_candidates(
    files: Sequence[_MemoryFile], index_text: str, now: datetime, retention: timedelta
) -> Iterable[_MemoryFile]:
    """Yield budget-tier archival candidates lowest-signal first, just enough to fit budget.

    Fires only when the live ``MEMORY.md`` is over budget. Each file is scored by
    :func:`_signal_score` and the lowest-signal files are archived first. A referenced
    file (a live consumer still ``[[link]]``s it) is NOT hard-retained here (#2753): the
    cross-link phase runs before decay and references most of the corpus, so a hard skip
    floored the tier above the referenced count and the index could never reach budget.
    Instead ``_signal_score`` adds +40 per inbound ``[[name]]`` link, so referenced
    entries rank HIGHER and are archived LAST — only when the budget genuinely forces it.
    After each removal the survivor set's PROJECTED index — rendered exactly as the
    re-index will render it — is re-measured, and the walk STOPS as soon as it is under
    the BYTE budget, so the MINIMUM number of (lowest-signal) files is
    archived and as much high-signal memory as fits stays hot. user / BINDING entries
    score highest and are archived only if the budget forces it. Every archived entry
    stays restorable (full body in ``archive/`` with provenance) and recall-able (its
    signature in the cold ``MEMORY_ARCHIVE.md``); a now-dangling ``[[link]]`` in a
    surviving body is cosmetic, not data loss — the hot index uses bare ``- name.md``
    pointers, which never dangle. The conservative stale/ledger tier
    (:func:`_stale_candidates`) keeps its reference skip — only the budget tier drops it.
    """
    if not _index_over_budget(index_text):
        return
    from teatree.loops.dream import reindex  # noqa: PLC0415 — deferred: loaded at tick time, not import

    inbound = _inbound_link_counts(files, index_text)
    ordered = sorted(
        files, key=lambda m: _signal_score(m, inbound_links=inbound.get(m.name, 0), now=now, retention=retention)
    )
    line_bytes = {m.path: len(reindex.index_line_for(m.path.name, m.text).encode("utf-8")) for m in files}
    header = reindex.render_index_lines([])
    header_bytes = len(header.encode("utf-8"))
    survivor_count = len(files)
    survivor_bytes = sum(line_bytes.values())
    for memory in ordered:
        # projected_bytes == len(render_index_lines(survivor lines).encode()) — exact for any
        # count (the per-line "\n" join + trailing newline total ``survivor_count`` bytes).
        projected_bytes = header_bytes + survivor_bytes + survivor_count
        if not _over_budget(projected_bytes):
            break  # projected survivor index is back under the byte budget — archive no more
        survivor_count -= 1
        survivor_bytes -= line_bytes[memory.path]
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
    (memory_dir / _ARCHIVE_INDEX_NAME).write_text(_COLD_HEADER + "\n".join(lines) + "\n", encoding="utf-8")


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
    files = _load_memory_files(memory_dir)
    index_path = memory_dir / _INDEX_NAME
    index_text = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
    retention = timedelta(days=settings.retention_days)
    archive_dir = memory_dir / ARCHIVE_DIRNAME

    home_tier = list(_stale_candidates(files, index_text, moment, retention, resolver))
    archived: list[ArchivedMemory] = [
        _archive_one(memory, archive_dir, moment, reason="stale, unreferenced, durably homed", dry_run=dry_run)
        for memory in home_tier
    ]
    if settings.budget_tier is not None:
        homed_paths = {m.path for m in home_tier}
        remaining = [m for m in files if m.path not in homed_paths]
        archived.extend(
            _archive_one(memory, archive_dir, moment, reason="over-budget, lowest-signal", dry_run=dry_run)
            for memory in _budget_tier_candidates(remaining, index_text, moment, retention)
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
