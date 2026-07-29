"""What an env dir's own owner stamp proves, and what THIS venue may conclude from it (#3872).

A checkout scan answers "did this venue find an owner", never "does an owner exist",
and the two are different answers on any box where the reaper and the checkouts live
in different filesystems. #3872 measured the gap in both directions on one host: the
container that the project mandates ``t3`` runs in has the isolated-env root bind-
mounted but not the host's clone, so it saw 185 env dirs and none of their owners and
correctly concluded every one was an orphan — from a false premise. Run the same
dry-run on the host and two dirs the container KEEPS as owned appear in the host's
removal list, because the host cannot see the container's own source volume. Neither
venue is wrong; they are looking at different trees.

So no rule of the form "my scan found no owner, therefore the owner is dead" is sound,
here or anywhere. The stamp is what replaces it: :class:`teatree.paths.IsolatedEnvDir`
records the owning checkout INSIDE the dir at birth, and that name is venue-independent
— it says the same thing read from the host or the container. What remains
venue-dependent is only whether this venue can check it, and
:func:`venue_can_observe` is that question asked explicitly rather than assumed.

Two conclusions follow, and the reaper draws exactly these:

*   a stamp naming a path this venue cannot observe is MISSING EVIDENCE, never proof
    of death — the dir is kept and the gap reported;
*   an UNSTAMPED dir is the same verdict with less to say: nothing recorded its owner,
    so nothing can retire it. Stamping is universal at birth, so this costs only the
    dirs that predate it — reclaimed by running :func:`backfill_owner_stamps` in each
    venue that can see checkouts, since neither venue sees them all.
"""

from dataclasses import dataclass
from pathlib import Path

from teatree import paths
from teatree.core.management.commands._workspace import checkout_registry


def venue_can_observe(path: Path, scanned_roots: tuple[Path, ...]) -> bool:
    """Whether this venue could have SEEN *path* had it existed.

    A venue earns the right to call a checkout dead by reading the directory that
    would hold it and finding it absent. Both halves are load-bearing: the path must
    lie under a root this venue walked at all, and that path's parent must be a
    directory this venue can list. The container case fails the second half — the
    stamp names ``<clone>/.claude/worktrees/<name>`` whose parent chain is simply not
    mounted, so what the venue observes is "an unmounted subtree", not "a deleted
    checkout". A parent that cannot be read at all reads as unobservable, so an
    unreadable neighbourhood is never mistaken for an empty one.
    """
    if not any(path.is_relative_to(root) for root in scanned_roots):
        return False
    return path.parent.is_dir()


@dataclass(frozen=True, slots=True)
class OwnerStamp:
    """The checkout an env dir names as its owner, weighed against what this venue sees."""

    owner: Path | None
    observable: bool

    @property
    def proof_of_life(self) -> str | None:
        """The evidence that the stamped owner is alive, or ``None`` when there is none."""
        if self.owner is not None and self.owner.is_dir():
            return f"its owner stamp names a live checkout ({self.owner})"
        return None

    @property
    def missing_evidence(self) -> str | None:
        """Why this venue cannot prove the dir is dead, or ``None`` when it can."""
        if self.owner is None:
            return "unstamped — no owner recorded, so nothing here can prove it dead (run `workspace stamp-owners`)"
        if not self.observable:
            return (
                f"its owner stamp names {self.owner}, which this venue cannot see"
                " — missing evidence, not proof of death"
            )
        return None


def read_owner_stamp(env_dir: Path, scanned_roots: tuple[Path, ...]) -> OwnerStamp:
    """*env_dir*'s recorded owner and whether this venue is in a position to judge it."""
    owner = paths.IsolatedEnvDir(env_dir).owner
    return OwnerStamp(owner, observable=owner is not None and venue_can_observe(owner, scanned_roots))


def stamp_discovered_owners(checkouts: frozenset[str], root: Path) -> list[str]:
    """Record each discovered checkout as the owner of its env dir, reporting new stamps.

    Backfill, so the invertible mapping covers what already exists rather than only
    dirs minted after the stamp shipped — it protected 3 of 185 dirs on the host until
    this ran. Writing it here also means a dir's ownership survives the checkout later
    becoming undiscoverable, which is precisely the state a second venue sees it in.
    """
    stamped: list[str] = []
    for checkout in sorted(checkouts):
        env_dir = root / paths.isolated_slug(Path(checkout))
        if not env_dir.is_dir():
            continue
        already = paths.IsolatedEnvDir(env_dir).owner
        paths.IsolatedEnvDir(env_dir).stamp_owner(Path(checkout))
        if already is None:
            stamped.append(f"Stamped '{env_dir.name}' as owned by {checkout}")
    return stamped


def backfill_owner_stamps(workspace: Path) -> list[str]:
    """Stamp every env dir whose owning checkout THIS venue can see — and nothing else.

    The non-destructive half of the reaper, split out so it can be run where
    ``clean-all`` must not be. Ownership evidence is venue-limited in both directions,
    so the durable mapping is only completed by running this in each venue that sees
    checkouts; a venue blind to a checkout simply contributes nothing about it, which
    is the correct null result rather than a deletion.
    """
    root = paths.auto_isolated_worktrees_dir()
    if not root.is_dir():
        return [f"No auto-isolated env root at {root} — nothing to stamp"]
    registry = checkout_registry.live_checkout_paths(workspace)
    stamped = stamp_discovered_owners(registry.paths, root)
    unstamped = sum(1 for env_dir in root.iterdir() if env_dir.is_dir() and paths.IsolatedEnvDir(env_dir).owner is None)
    report = [*stamped, f"Stamped {len(stamped)} env dir(s) from {len(registry.paths)} checkout(s) this venue can see"]
    report.append(
        f"{unstamped} env dir(s) still unstamped — run this in every venue that can see checkouts"
        if unstamped
        else "Every env dir now names its owner"
    )
    report.extend(f"GAP: {gap}" for gap in registry.gaps)
    return report


__all__ = [
    "OwnerStamp",
    "backfill_owner_stamps",
    "read_owner_stamp",
    "stamp_discovered_owners",
    "venue_can_observe",
]
