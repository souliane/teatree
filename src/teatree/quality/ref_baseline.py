"""The two shrink-only reference ratchets' pinned baseline, held as data.

``known_unresolved_refs.yaml`` next to this module is the source of truth for
both ratchets asserted in ``tests/teatree_quality/test_skill_symbol_refs.py``:
``charter`` (the documents an agent loads before any skill) and ``python_prose``
(docstrings and ``#:`` comments under the indexed packages). Each pins the
``(file, reference)`` pairs that do not resolve today, and each is asserted in
BOTH directions — a NEW unresolved reference reds, and a pinned one that stopped
being reported reds until its entry is deleted.

The baseline lives here rather than as a literal in the test because the repair
has to be mechanical. A pin goes stale for reasons outside its own PR: #4451's
``main`` outage was two PRs off a shared base, one seeding a pin while the other
rewrote the citation it named, each green alone and red merged. Deleting a stale
pin is the whole fix, and it is derivable — so it is a data write, not a
regex over Python source.

Only the STALE direction is ever repaired automatically. Deleting a pin makes the
guard strictly tighter, so it cannot mask a defect; ADDING one loosens it and is
always a human decision. :func:`prune` therefore only ever removes, and refuses
to write a baseline that grew.
"""

from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml

from teatree.quality.python_prose_refs import scan_python_tree
from teatree.quality.skill_symbol_refs import SymbolRefFinding, scan_file

Ratchet = Literal["charter", "python_prose"]
RATCHETS: tuple[Ratchet, ...] = ("charter", "python_prose")

Pins = frozenset[tuple[str, str]]
Baseline = dict[Ratchet, Pins]

#: Charter documents load before any skill, so a stale citation in one reads as a
#: work item rather than as documentation drift.
_CHARTER_FILES: tuple[str, ...] = ("BLUEPRINT.md", "AGENTS.md", "CLAUDE.md")
_CHARTER_GLOBS: tuple[str, ...] = ("docs/blueprint/*.md",)

_HEADER = """\
# Pinned unresolved references for the two shrink-only ratchets asserted in
# tests/teatree_quality/test_skill_symbol_refs.py. Source of truth: this file.
#
# Each entry is a (file, reference) pair the scanner reports as unresolved today.
# The set may only ever SHRINK. Fixing a citation means deleting its entry here in
# the SAME commit — the staleness half fires on "no longer reported", so a fix
# without the deletion reds main.
#
# Run `t3 tool ratchet-prune --check` to see which entries went stale, and
# `t3 tool ratchet-prune --write` to delete exactly those. The tool never adds an
# entry: a genuinely new unresolved reference is a citation to fix or a
# `skill-symbol-ref:` pragma to add, never a pin to bank.
"""


class BaselineError(ValueError):
    """The baseline file is missing, unreadable, or not shaped as two ratchet maps."""

    def __init__(self, location: str | None, message: str) -> None:
        super().__init__(f"{location}: {message}" if location else message)


def baseline_path() -> Path:
    return Path(__file__).parent / "known_unresolved_refs.yaml"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _coerce_ratchet(name: str, raw: object) -> Pins:
    where = f"ratchet {name!r}"
    if not isinstance(raw, Mapping):
        raise BaselineError(where, f"expected a mapping of file -> [reference], got {type(raw).__name__}")
    pins: set[tuple[str, str]] = set()
    for file_key, refs in raw.items():
        if not isinstance(file_key, str) or not isinstance(refs, list):
            raise BaselineError(where, f"entry {file_key!r} must map a path to a list of references")
        for ref in refs:
            if not isinstance(ref, str):
                raise BaselineError(where, f"entry {file_key!r} holds a non-string reference {ref!r}")
            pins.add((file_key, ref))
    return frozenset(pins)


def load_baseline(path: Path | None = None) -> Baseline:
    """Read the pinned baseline, failing loud rather than degrading to an empty set.

    An empty ratchet would silently satisfy the staleness half while making the
    new-unresolved half red on every pre-existing pin, so a read error must never
    present as "nothing is pinned".
    """
    source = path or baseline_path()
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BaselineError(str(source), f"could not read the reference baseline: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise BaselineError(str(source), "expected a top-level mapping of ratchet name -> entries")
    missing = [name for name in RATCHETS if name not in raw]
    if missing:
        raise BaselineError(str(source), f"missing ratchet(s) {missing} — every ratchet must be present, even if empty")
    return {name: _coerce_ratchet(name, raw[name]) for name in RATCHETS}


def dump_baseline(baseline: Baseline, path: Path | None = None) -> None:
    """Write the baseline back, grouped by file and sorted so a prune diffs minimally."""
    grouped: dict[str, dict[str, list[str]]] = {}
    for name in RATCHETS:
        by_file: dict[str, list[str]] = {}
        for file_key, ref in sorted(baseline[name]):
            by_file.setdefault(file_key, []).append(ref)
        grouped[name] = by_file
    body = yaml.safe_dump(grouped, sort_keys=True, default_flow_style=False, width=100, allow_unicode=True)
    (path or baseline_path()).write_text(_HEADER + body, encoding="utf-8")


def charter_docs(root: Path) -> list[Path]:
    """The documents the charter ratchet walks, in a stable order."""
    docs = [root / name for name in _CHARTER_FILES]
    for pattern in _CHARTER_GLOBS:
        docs.extend(sorted(root.glob(pattern)))
    return docs


def _pairs(root: Path, findings: Iterable[SymbolRefFinding]) -> set[tuple[str, str]]:
    return {(str(f.path.relative_to(root)), f.ref) for f in findings if f.reason is not None}


@lru_cache(maxsize=8)
def _reported_cached(ratchet: Ratchet, root: Path) -> Pins:
    """The whole-tree walk, run once per (ratchet, root) rather than once per caller."""
    if ratchet == "charter":
        findings = [f for doc in charter_docs(root) if doc.is_file() for f in scan_file(doc, root)]
    else:
        findings = scan_python_tree(root)
    return frozenset(_pairs(root, findings))


def reported_refs(ratchet: Ratchet, root: Path | None = None) -> set[tuple[str, str]]:
    """Every ``(file, reference)`` pair the scanner reports as unresolved right now."""
    return set(_reported_cached(ratchet, root or repo_root()))


def stale_entries(root: Path | None = None, *, path: Path | None = None) -> Baseline:
    """Per ratchet, the pinned pairs the scanner no longer reports — the auto-repairable set."""
    baseline = load_baseline(path)
    return {name: frozenset(baseline[name] - reported_refs(name, root)) for name in RATCHETS}


def new_entries(root: Path | None = None, *, path: Path | None = None) -> Baseline:
    """Per ratchet, the unresolved pairs no entry covers — reported, never auto-banked."""
    baseline = load_baseline(path)
    return {name: frozenset(reported_refs(name, root) - baseline[name]) for name in RATCHETS}


def prune(root: Path | None = None, *, path: Path | None = None, write: bool = False) -> Baseline:
    """Delete every stale pin; return what was (or would be) removed.

    Shrink-only by construction: the written baseline is the loaded one minus the
    stale set, so no call can ever add an entry.
    """
    baseline = load_baseline(path)
    stale = stale_entries(root, path=path)
    if write and any(stale.values()):
        dump_baseline({name: frozenset(baseline[name] - stale[name]) for name in RATCHETS}, path)
    return stale


__all__ = [
    "RATCHETS",
    "Baseline",
    "BaselineError",
    "Pins",
    "Ratchet",
    "baseline_path",
    "charter_docs",
    "dump_baseline",
    "load_baseline",
    "new_entries",
    "prune",
    "repo_root",
    "reported_refs",
    "stale_entries",
]
