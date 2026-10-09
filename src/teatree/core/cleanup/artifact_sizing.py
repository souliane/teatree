"""Sizing the eligible artifacts, largest first, inside the pass's cap and its time budget (#4244)."""

import os
import time
from dataclasses import dataclass
from pathlib import Path

#: How many artifacts one pass may evict. A bound on the blast radius, not a
#: coverage claim — the count dropped is reported. Raised with the name set, which
#: went from two names to five.
MAX_EVICTIONS_PER_PASS = 50

#: How many eligible artifacts one pass may SIZE. Sizing is an ``os.walk`` per
#: candidate over trees reaching 100k inodes, and it runs INLINE in the tick beside
#: the pressure measurement and the intake sizing on the same mini-loop. Ordering the
#: whole eligible set by size therefore paid for a walk of every candidate it was
#: going to defer as well — on a box with a few hundred checkouts up to ~1200 walks per pass, with the
#: remainder re-walked on every pass until the backlog drained. The prefix is
#: alphabetical, NOT a sample of the biggest — on a box with more eligible artifacts than
#: this bound, the largest one is likely outside it and waits for a later pass. What makes
#: that acceptable is that the ordering is deterministic and the backlog drains: what a
#: pass evicts leaves the eligible set, so the next pass reaches further in.
MAX_SIZED_PER_PASS = 200


@dataclass(frozen=True, slots=True)
class ArtifactCandidate:
    """One dormant build artifact and what removing it would return."""

    artifact: Path
    checkout: Path
    size_bytes: int
    artifact_identity: tuple[int, int, int] | None = None
    checkout_identity: tuple[int, int, int] | None = None


def path_identity(path: Path) -> tuple[int, int, int] | None:
    try:
        value = path.lstat()
    except OSError:
        return None
    return value.st_dev, value.st_ino, value.st_mode


def largest_first(
    eligible: list[tuple[Path, Path]], *, deadline: float | None
) -> tuple[tuple[ArtifactCandidate, ...], list[str]]:
    """Spend the cap's budget on the biggest of a BOUNDED prefix, and say what that deferred.

    First-come selection was near-enough arbitrary over two names per checkout; over five
    names across hundreds of checkouts it lets one pass burn its whole budget on kilobyte
    candidates while the multi-gigabyte ones are deferred forever. So the accepted set is
    size-ordered — but ordering the WHOLE eligible set means sizing every candidate the
    pass is about to defer, and sizing is the expensive part (see
    :data:`MAX_SIZED_PER_PASS`).

    The prefix is ALPHABETICAL, so "the biggest" means the biggest of that prefix rather
    than of the box: past the sizing bound the largest artifact waits for a later pass.
    Determinism is what makes that drain rather than churn — the same prefix is sized each
    pass, the evicted ones leave the eligible set, and the next pass reaches further in.
    """
    ordered = sorted(eligible)
    measurable, unsized = ordered[:MAX_SIZED_PER_PASS], ordered[MAX_SIZED_PER_PASS:]
    sized: list[ArtifactCandidate] = []
    unmeasured: list[Path] = []
    for artifact, checkout in measurable:
        if budget_spent(deadline):
            unmeasured.append(artifact)
            continue
        sized.append(
            ArtifactCandidate(
                artifact, checkout, _dir_size_bytes(artifact), path_identity(artifact), path_identity(checkout)
            )
        )
    sized.sort(key=lambda candidate: candidate.size_bytes, reverse=True)
    deferred = [
        f"{candidate.artifact}: over the {MAX_EVICTIONS_PER_PASS}-per-pass cap, deferred to the next pass"
        for candidate in sized[MAX_EVICTIONS_PER_PASS:]
    ]
    deferred += [f"{artifact}: the pass's time budget ran out before it was sized" for artifact in unmeasured]
    deferred += [
        f"{artifact}: over the {MAX_SIZED_PER_PASS}-per-pass sizing bound, not measured this pass"
        for artifact, _checkout in unsized
    ]
    return tuple(sized[:MAX_EVICTIONS_PER_PASS]), deferred


def _dir_size_bytes(directory: Path) -> int:
    """Bytes this tree owns — never a byte reached through a link out of it.

    ``followlinks=False`` is the default and is stated so the next reader does not
    "simplify" it away: a ``node_modules`` is full of internal links (``.bin/*``,
    workspace links) whose targets sit outside the tree being sized. A link's own
    inode is not credited either, so the figure is what deleting the tree returns.
    """
    total = 0
    for root, _, files in os.walk(directory, followlinks=False):
        for name in files:
            entry = Path(root) / name
            try:
                if not entry.is_symlink():
                    total += entry.lstat().st_size
            except OSError:
                continue
    return total


def budget_spent(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


__all__ = [
    "MAX_EVICTIONS_PER_PASS",
    "MAX_SIZED_PER_PASS",
    "ArtifactCandidate",
    "budget_spent",
    "largest_first",
    "path_identity",
]
