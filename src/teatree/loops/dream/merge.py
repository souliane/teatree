"""Phase 4b of the dream pass — merge near-duplicate memory files (#2723).

The memory set grows by hand; over time two ``feedback_*.md`` files end up
recording the SAME lesson with slightly different wording. Cross-link (phase 4)
detects relatedness and links them, but keeps BOTH — so the corpus never shrinks
and the index keeps bloating. This phase is the missing MERGE verb: it collapses
a near-duplicate PAIR into one survivor, preserving every distinct lesson.

A pair is a near-duplicate when its topic-token Jaccard is at or above a HIGH
floor (:data:`_NEAR_DUPLICATE_FLOOR`, far above cross-link's 0.18) AND the two
files are the same frontmatter ``type``/``name`` FAMILY — so a feedback rule and
a reference note about the same subsystem are never collapsed, only true twins.

The merge mirrors decay's transfer-before-prune rail: the HIGHER-WEIGHT file is
the survivor (a BINDING rule outranks a plain restatement, so binding doctrine is
never lost), the absorbed file's distinct lines are appended to the survivor with
a provenance header, and the absorbed file is ARCHIVED via
:func:`teatree.loops.dream.decay._archive_one` (moved to ``archive/``, never
deleted). The reindex pass then drops the absorbed pointer from ``MEMORY.md``.

Decision-3 (binding): two BINDING near-duplicates are NEVER auto-merged — two
load-bearing rules that disagree cannot be silently collapsed. Such a pair is
cross-linked and surfaced as a :class:`BindingConflict` for human reconciliation
(the command files a ``dream-memory-gap`` ticket via the existing filer).

PURE and idempotent: a re-run on a set with no near-duplicates merges nothing.
NEVER touches the real ``~/.claude``: the caller passes an explicit ``memory_dir``;
a missing dir is a clean no-op. Fault-isolated by the command's try/except.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from teatree.loops.dream.cross_link import _jaccard, _topic_tokens
from teatree.loops.dream.decay import _archive_one, _load_memory_files, _MemoryFile
from teatree.loops.dream.reindex import _strip_frontmatter

#: A pair is a near-duplicate (mergeable) only above this HIGH Jaccard floor —
#: far above cross-link's 0.18 relatedness floor, so only true twins collapse.
_NEAR_DUPLICATE_FLOOR = 0.85

_WIKILINK_PREFIX = "Related:"

#: Weight floors mirroring the engine's member ranking, applied to a file body +
#: name so the higher-signal file of a pair is the survivor. A BINDING rule
#: outranks a ``feedback_*`` file, which outranks a retro finding, then anything
#: else — so binding doctrine is never the side that gets absorbed.
_WEIGHT_BINDING = 100
_WEIGHT_FEEDBACK = 90
_WEIGHT_RETRO = 70
_WEIGHT_OTHER = 10


@dataclass(frozen=True, slots=True)
class MergedMemory:
    """One near-duplicate pair the merge phase collapsed."""

    survivor_name: str
    absorbed_name: str
    archive_path: Path


@dataclass(frozen=True, slots=True)
class BindingConflict:
    """Two BINDING near-duplicates that were cross-linked, NOT merged (Decision-3)."""

    survivor_name: str
    absorbed_name: str
    survivor_path: Path
    absorbed_path: Path


@dataclass(frozen=True, slots=True)
class MergeResult:
    """What one phase-4b pass did: files seen, pairs merged, binding conflicts, dry."""

    seen: int
    merged: tuple[MergedMemory, ...]
    binding_conflicts: tuple[BindingConflict, ...]
    dry_run: bool

    @property
    def merged_count(self) -> int:
        return len(self.merged)


def _file_weight(memory: _MemoryFile) -> int:
    body = memory.text.lower()
    name = memory.path.name.lower()
    if "binding" in body:
        return _WEIGHT_BINDING
    if name.startswith("feedback_"):
        return _WEIGHT_FEEDBACK
    if "retro" in name or "retro finding" in body:
        return _WEIGHT_RETRO
    return _WEIGHT_OTHER


def _is_binding(memory: _MemoryFile) -> bool:
    return "binding" in memory.text.lower()


def _family(memory: _MemoryFile) -> str:
    """The ``type``/``name`` family two files must share to be mergeable.

    Reads a frontmatter ``type:`` field; absent one, falls back to the filename's
    leading token (``feedback`` from ``feedback_x.md``). Two files in different
    families are never collapsed even when their topics overlap.
    """
    for raw in memory.text.splitlines():
        line = raw.strip().lower()
        if line.startswith("type:"):
            return line.split(":", 1)[1].strip()
    return memory.path.stem.split("_", 1)[0].lower()


def _body_tokens(memory: _MemoryFile) -> frozenset[str]:
    """Topic tokens of the memory BODY (frontmatter stripped).

    The near-dup decision compares lesson CONTENT, not metadata: two twins carry
    different ``name``/``summary`` frontmatter, so tokenizing the whole file would
    let those differing metadata tokens dilute a real near-duplicate below the
    floor. The body is where the lesson lives.
    """
    _front, body = _strip_frontmatter(memory.text)
    return _topic_tokens(body)


@dataclass(frozen=True, slots=True)
class _Pair:
    survivor: _MemoryFile
    absorbed: _MemoryFile


def _near_duplicate_pairs(files: Sequence[_MemoryFile]) -> list[_Pair]:
    """The disjoint near-duplicate pairs to collapse, highest-weight survivor first.

    Greedy + disjoint: each file is merged at most once per pass (the survivor of
    one pair is not re-absorbed into another), so a re-run after the absorbed file
    is archived finds no further pair — the idempotence the test pins.
    """
    pairs: list[_Pair] = []
    consumed: set[Path] = set()
    tokens = {f.path: _body_tokens(f) for f in files}
    for i, left in enumerate(files):
        if left.path in consumed:
            continue
        for right in files[i + 1 :]:
            if right.path in consumed:
                continue
            if _family(left) != _family(right):
                continue
            if _jaccard(tokens[left.path], tokens[right.path]) < _NEAR_DUPLICATE_FLOOR:
                continue
            survivor, absorbed = _order_by_weight(left, right)
            pairs.append(_Pair(survivor=survivor, absorbed=absorbed))
            consumed.add(left.path)
            consumed.add(right.path)
            break
    return pairs


def _order_by_weight(a: _MemoryFile, b: _MemoryFile) -> tuple[_MemoryFile, _MemoryFile]:
    """Return (survivor, absorbed): the higher-weight file survives; ties keep *a*."""
    return (a, b) if _file_weight(a) >= _file_weight(b) else (b, a)


def _distinct_lines(survivor: _MemoryFile, absorbed: _MemoryFile) -> list[str]:
    """The absorbed file's body lines not already present in the survivor."""
    have = {line.strip() for line in survivor.text.splitlines() if line.strip()}
    return [raw for raw in absorbed.text.splitlines() if raw.strip() and raw.strip() not in have]


def _merge_provenance(absorbed: _MemoryFile, now: datetime) -> str:
    return (
        f"\n<!-- merged in {absorbed.path.name} by dream merge {now.date().isoformat()}; "
        f"its distinct content follows -->\n"
    )


def _apply_merge(pair: _Pair, archive_dir: Path, now: datetime, *, dry_run: bool) -> MergedMemory:
    """Archive the absorbed file FIRST, then append its distinct lines to the survivor.

    Archive-first keeps the two-file mutation crash-safe: if the process dies after
    the absorbed file is archived but before the survivor is rewritten, the absorbed
    lesson still lives (in ``archive/``, body preserved) and the pair is never
    re-paired next pass (the absorbed file is gone from the live dir). The reverse
    order — rewrite survivor, then archive — left BOTH files live on a kill, so the
    next pass re-paired them and re-appended the same content.
    """
    distinct = _distinct_lines(pair.survivor, pair.absorbed)
    archived = _archive_one(
        pair.absorbed, archive_dir, now, reason=f"merged into {pair.survivor.path.name}", dry_run=dry_run
    )
    if not dry_run:
        existing = pair.survivor.path.read_text(encoding="utf-8")
        suffix = "" if existing.endswith("\n") else "\n"
        addition = _merge_provenance(pair.absorbed, now) + "\n".join(distinct) + ("\n" if distinct else "")
        pair.survivor.path.write_text(f"{existing}{suffix}{addition}", encoding="utf-8")
    return MergedMemory(
        survivor_name=pair.survivor.path.stem,
        absorbed_name=pair.absorbed.path.stem,
        archive_path=archived.destination,
    )


def _record_binding_conflict(pair: _Pair, *, dry_run: bool) -> BindingConflict:
    """Return the two-BINDING conflict (Decision-3); cross-link the files unless dry-run.

    The conflict is ALWAYS reported (so a dry run previews the binding conflicts a real
    run would surface, not zero); only the cross-link side effect is skipped under
    *dry_run*.
    """
    if not dry_run:
        _ensure_wikilink(pair.survivor.path, pair.absorbed.path.stem)
        _ensure_wikilink(pair.absorbed.path, pair.survivor.path.stem)
    return BindingConflict(
        survivor_name=pair.survivor.path.stem,
        absorbed_name=pair.absorbed.path.stem,
        survivor_path=pair.survivor.path,
        absorbed_path=pair.absorbed.path,
    )


def _ensure_wikilink(path: Path, target_stem: str) -> None:
    text = path.read_text(encoding="utf-8")
    if f"[[{target_stem}]]" in text:
        return
    suffix = "" if text.endswith("\n") else "\n"
    path.write_text(f"{text}{suffix}{_WIKILINK_PREFIX} [[{target_stem}]]\n", encoding="utf-8")


def merge_memories(memory_dir: Path, *, now: datetime | None = None, dry_run: bool = False) -> MergeResult:
    """Collapse near-duplicate memory pairs under *memory_dir*; preserve every lesson.

    A pair above the HIGH near-duplicate floor AND in the same ``type``/``name``
    family is merged into the higher-weight survivor (a BINDING rule outranks a
    plain restatement); the absorbed file's distinct lines are appended with a
    provenance header and the absorbed file is ARCHIVED (moved, never deleted). Two
    BINDING near-duplicates are NOT merged — they are cross-linked and returned as
    :class:`BindingConflict` for human reconciliation (Decision-3). A missing dir is
    a clean no-op. Under *dry_run* the decision is computed but nothing moves.
    """
    moment = now or datetime.now(tz=UTC)
    if not memory_dir.is_dir():
        return MergeResult(seen=0, merged=(), binding_conflicts=(), dry_run=dry_run)
    files = _load_memory_files(memory_dir)
    archive_dir = memory_dir / "archive"
    merged: list[MergedMemory] = []
    conflicts: list[BindingConflict] = []
    for pair in _near_duplicate_pairs(files):
        if _is_binding(pair.survivor) and _is_binding(pair.absorbed):
            conflicts.append(_record_binding_conflict(pair, dry_run=dry_run))
            continue
        merged.append(_apply_merge(pair, archive_dir, moment, dry_run=dry_run))
    return MergeResult(seen=len(files), merged=tuple(merged), binding_conflicts=tuple(conflicts), dry_run=dry_run)


__all__ = ["BindingConflict", "MergeResult", "MergedMemory", "merge_memories"]
