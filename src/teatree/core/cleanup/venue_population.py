"""What an in-loop reclaimer may delete, and which checkouts' links protect it — two populations.

CANDIDATES are the checkouts under the roots this venue provisions into: a walk that stops at
each checkout, plus the worktrees the clones found there register under those roots. It is
bounded on purpose — from the container, home and a registered row's parent reach the host
workspace through a Docker Desktop file share, where the walk outlived the tick deadline and
where a host agent's working directory is invisible to the process table.

PROTECTORS are every checkout whose artifact link could point at a candidate: the candidates,
every worktree those clones register WHEREVER it lives, and the standalone clones nested
inside a candidate. Narrowing the candidates never narrows the protectors, because a shared
target is protected by links the candidate population may not contain.

Membership compares RESOLVED spellings: git records a worktree under the path it was created
with, which from the container is often the host's real path rather than the venue's alias.
"""

import dataclasses
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from teatree.config import clone_root
from teatree.core.cleanup.artifact_lock import artifact_name_pattern
from teatree.core.cleanup.checkout_registry import (
    CheckoutRegistry,
    excluded_from_walk,
    one_spelling_each,
    outermost_roots,
    raw_worktree_paths,
    scan_checkout_paths,
)
from teatree.core.worktree.worktree_roots import canonical_worktree_root, registered_worktree_paths
from teatree.utils import git
from teatree.utils.run import CommandFailedError

#: Git subprocesses are latency-bound on a file share, not CPU-bound.
_PROBE_WORKERS = 8
#: Clones nested inside nested clones are followed this many levels, then recorded as a gap.
_NESTED_DEPTH = 3
_SUBMODULE_PATH = re.compile(r"^\s*path\s*=\s*(.+?)\s*$", re.MULTILINE)

type Identity = tuple[int, int]


@dataclass(frozen=True, slots=True)
class VenuePopulation:
    """The checkouts a pass may delete from, the ones whose links protect them, and what went unread."""

    candidates: frozenset[str]
    protectors: frozenset[str]
    clones: tuple[Path, ...]
    gaps: tuple[str, ...]
    roots: tuple[Path, ...]
    #: Registered checkouts that are protectors but never candidates, each with why.
    excluded: tuple[str, ...] = ()
    #: The directories between checkouts the candidate walk listed.
    listed: tuple[Path, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.gaps


def venue_checkout_roots(workspace: Path) -> tuple[Path, ...]:
    """The roots THIS venue provisions checkouts into — never home, never a row's parent."""
    return outermost_roots({clone_root(), canonical_worktree_root(), workspace})


def venue_candidates(workspace: Path, *, deadline: float | None = None) -> VenuePopulation:
    """The bounded candidate population, with the registry-reported worktrees as protectors."""
    roots = venue_checkout_roots(workspace)
    scan = scan_checkout_paths(roots, into_checkouts=False, deadline=deadline)
    candidates = set(scan.paths)
    protectors = set(scan.paths)
    gaps = list(scan.gaps)
    excluded: list[str] = []
    clones: list[Path] = []
    for checkout in one_spelling_each(scan.paths):
        try:
            if not (checkout / ".git").is_dir():
                continue
            registered = raw_worktree_paths(str(checkout))
        except (CommandFailedError, OSError) as exc:
            gaps.append(f"clone {checkout}: could not list worktrees ({exc})")
            continue
        clones.append(checkout)
        for path in registered:
            protectors.add(path)
            if _within(Path(path), roots):
                candidates.add(path)
            else:
                excluded.append(f"{path}: registered by {checkout} outside {_listed(roots)} — a protector only")
    for checkout in one_spelling_each(frozenset(candidates)):
        gaps.extend(_submodule_gaps(checkout))
    excluded.extend(_out_of_venue_rows(roots, candidates))
    return VenuePopulation(
        frozenset(candidates), frozenset(protectors), tuple(clones), tuple(gaps), roots, tuple(excluded), scan.listed
    )


def venue_population(workspace: Path, *, deadline: float | None = None) -> VenuePopulation:
    """:func:`venue_candidates` with the standalone clones nested inside a candidate added as protectors.

    The probe reads every source tree, so it is skipped when the candidate walk already has a
    gap: the pass refuses on that gap whatever the probe would find.
    """
    base = venue_candidates(workspace, deadline=deadline)
    if base.gaps:
        return base
    nested, gaps = nested_clones(one_spelling_each(base.candidates), deadline=deadline)
    return dataclasses.replace(base, protectors=base.protectors | nested, gaps=base.gaps + gaps)


def linked_worktree_paths(workspace: Path, *, deadline: float | None = None) -> CheckoutRegistry:
    """Every LINKED worktree among this venue's candidates — the population a worktree GC may act on.

    Main clones and the ad-hoc checkouts that are nobody's worktree are excluded, so a caller
    that removes what it is handed can never be handed a clone.
    """
    population = venue_candidates(workspace, deadline=deadline)
    found: set[str] = set()
    gaps = list(population.gaps)
    for path in one_spelling_each(population.candidates):
        try:
            linked = (path / ".git").is_file()
        except OSError as exc:
            gaps.append(f"could not classify {path}'s .git entry ({exc})")
            continue
        if linked:
            found.add(str(path))
    return CheckoutRegistry(frozenset(found), tuple(gaps), population.roots)


def nested_clones(checkouts: list[Path], *, deadline: float | None) -> tuple[frozenset[str], tuple[str, ...]]:
    """Checkouts nested inside *checkouts* that no registry reports, found through git's untracked listing."""
    seen = {str(checkout) for checkout in checkouts}
    found: set[str] = set()
    gaps: list[str] = []
    frontier = list(checkouts)
    for _ in range(_NESTED_DEPTH):
        if not frontier:
            break
        with ThreadPoolExecutor(max_workers=_PROBE_WORKERS) as pool:
            results = list(pool.map(lambda checkout: _probe(checkout, deadline=deadline), frontier))
        frontier = []
        for nested, probe_gaps in results:
            gaps.extend(probe_gaps)
            fresh = nested - seen
            seen |= fresh
            found |= fresh
            frontier.extend(Path(path) for path in fresh)
    gaps.extend(f"checkouts nested over {_NESTED_DEPTH} levels deep under {path} were not probed" for path in frontier)
    return frozenset(found), tuple(gaps)


def _probe(checkout: Path, *, deadline: float | None) -> tuple[set[str], list[str]]:
    if deadline is not None and time.monotonic() >= deadline:
        return set(), [f"walk budget exhausted before {checkout} was probed for nested clones"]
    try:
        listing = git.run_strict_verbatim(repo=str(checkout), args=["ls-files", "-z", "--others", "--directory"])
    except (CommandFailedError, OSError) as exc:
        return set(), [f"could not probe {checkout} for nested clones ({exc})"]
    untracked_dirs = (checkout / entry.rstrip("/") for entry in listing.split("\0") if entry.endswith("/"))
    tops = tuple(top for top in untracked_dirs if not _skipped(top))
    if not tops:
        return set(), []
    scan = scan_checkout_paths(tops, into_checkouts=False, deadline=deadline)
    return set(scan.paths), list(scan.gaps)


def _skipped(top: Path) -> bool:
    return artifact_name_pattern(top.name) is not None or excluded_from_walk(top)


def _submodule_gaps(checkout: Path) -> list[str]:
    """The walk does not descend to meet a submodule, so its parent's ``.gitmodules`` names it."""
    try:
        declared = (checkout / ".gitmodules").read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        return [f"could not read {checkout / '.gitmodules'} ({exc})"]
    return [
        f"submodule checkout {checkout / path} is nested under {checkout}"
        for path in _SUBMODULE_PATH.findall(declared)
        if (checkout / path / ".git").exists()
    ]


def _out_of_venue_rows(roots: tuple[Path, ...], candidates: set[str]) -> list[str]:
    """Why each registered row outside the roots is not a candidate: the same directory as one, or not ours."""
    outside = {pk: path for pk, path in registered_worktree_paths().items() if not _within(path, roots)}
    if not outside:
        return []
    names = {path.name for path in outside.values()}
    by_identity: dict[Identity, str] = {}
    for checkout in sorted(candidates):
        if Path(checkout).name in names and (identity := _identity(Path(checkout))):
            by_identity.setdefault(identity, checkout)
    notes: list[str] = []
    for pk, path in sorted(outside.items()):
        identity = _identity(path)
        if identity is not None and (venue_spelling := by_identity.get(identity)):
            notes.append(f"worktree row #{pk} at {path}: resolved to {venue_spelling}, the same directory")
        else:
            notes.append(
                f"worktree row #{pk} at {path}: skipped — outside {_listed(roots)} and not the same "
                "directory as any checkout under them"
            )
    return notes


def _identity(path: Path) -> Identity | None:
    try:
        status = path.stat()
    except OSError:
        return None
    return status.st_dev, status.st_ino


def _within(path: Path, roots: tuple[Path, ...]) -> bool:
    spellings = {path, _resolved(path)}
    bounds = {*roots, *(_resolved(root) for root in roots)}
    return any(spelling.is_relative_to(bound) for spelling in spellings for bound in bounds)


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def _listed(roots: tuple[Path, ...]) -> str:
    return ", ".join(str(root) for root in roots)


__all__ = [
    "VenuePopulation",
    "linked_worktree_paths",
    "nested_clones",
    "venue_candidates",
    "venue_checkout_roots",
    "venue_population",
]
