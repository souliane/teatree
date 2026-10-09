"""Where the protector population's artifact links resolve — the set no eviction may touch.

Matched by inode, not by path. The container reaches one host directory under several
spellings (a bind mount of the whole host workspace beside a bind of one subtree), and a
link written on the host names the host spelling, so a string comparison let a link protect
nothing its container-spelled target was known by. A link protects its target AND every
ancestor of it, because deleting either dangles the link.

Built once per pass and refreshed before each deletion by re-reading only what changed: a
checkout whose directory mtime moved (a link planted or replaced at its root), a clone
whose worktree registry moved (a new worktree), and a directory between checkouts whose
mtime moved (a new checkout beside the others, whose new child directories are scanned). The overlay's link writer holds
``artifact_source_lock`` on the target until the link exists, and eviction holds it through
the deletion, so a refresh under the lock sees every link the lock lets through.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from teatree.core.cleanup.artifact_lock import artifact_name_pattern
from teatree.core.cleanup.checkout_registry import child_directories, raw_worktree_paths, scan_checkout_paths
from teatree.core.worktree.checkout_gitdir import checkout_of_admin_entry
from teatree.utils.run import CommandFailedError

type Identity = tuple[int, int]


@dataclass(slots=True)
class _Reading:
    mtime_ns: int | None
    targets: frozenset[Identity] = frozenset()


@dataclass(slots=True)
class ArtifactProtection:
    """The identities every protector's artifact links reach, and what could not be read."""

    _checkouts: dict[Path, _Reading] = field(default_factory=dict)
    _registries: dict[Path, int | None] = field(default_factory=dict)
    _listed: dict[Path, int | None] = field(default_factory=dict)
    _identities: dict[Path, Identity | None] = field(default_factory=dict)
    _locked: frozenset[Path] | None = None
    gaps: list[str] = field(default_factory=list)

    @classmethod
    def read(
        cls, protectors: frozenset[str], clones: tuple[Path, ...], listed: tuple[Path, ...] = ()
    ) -> "ArtifactProtection":
        protection = cls()
        for clone in clones:
            protection._registries[clone] = _mtime_ns(clone / ".git" / "worktrees")
        for directory in listed:
            protection._listed[directory] = _mtime_ns(directory)
        for path in sorted(protectors):
            protection._read(Path(path))
        return protection

    def protects(self, identity: Identity) -> bool:
        return any(identity in reading.targets for reading in self._checkouts.values())

    def refresh(self) -> tuple[str, ...]:
        """Re-read what changed since the last reading; returns the gaps this refresh found."""
        before = len(self.gaps)
        self._locked = None
        self._rescan_moved_directories()
        for clone, seen in list(self._registries.items()):
            current = _mtime_ns(clone / ".git" / "worktrees")
            if current == seen:
                continue
            self._registries[clone] = current
            try:
                registered = raw_worktree_paths(str(clone))
            except (CommandFailedError, OSError) as exc:
                self.gaps.append(f"clone {clone}: could not list worktrees ({exc})")
                continue
            for path in registered:
                if Path(path) not in self._checkouts:
                    self._read(Path(path))
        for checkout, reading in list(self._checkouts.items()):
            if _mtime_ns(checkout) != reading.mtime_ns:
                self._read(checkout)
        return tuple(self.gaps[before:])

    def _rescan_moved_directories(self) -> None:
        for directory, seen in list(self._listed.items()):
            current = _mtime_ns(directory)
            if current == seen:
                continue
            self._listed[directory] = current
            children, gaps = child_directories(directory)
            self.gaps.extend(gaps)
            fresh = tuple(child for child in children if child not in self._listed and child not in self._checkouts)
            if not fresh:
                continue
            scan = scan_checkout_paths(fresh, into_checkouts=False)
            self.gaps.extend(scan.gaps)
            for listed in scan.listed:
                self._listed.setdefault(listed, _mtime_ns(listed))
            for checkout in scan.paths:
                if Path(checkout) not in self._checkouts:
                    self._read(Path(checkout))

    def _read(self, checkout: Path) -> None:
        mtime = _mtime_ns(checkout)
        try:
            with os.scandir(checkout) as entries:
                links = [
                    Path(entry.path) for entry in entries if artifact_name_pattern(entry.name) and entry.is_symlink()
                ]
        except FileNotFoundError:
            if reason := self._absence_unproven(checkout):
                self.gaps.append(f"{checkout} is absent here but {reason}")
            self._checkouts[checkout] = _Reading(mtime)
            return
        except OSError as exc:
            self.gaps.append(f"could not read the artifact links of {checkout} ({exc})")
            self._checkouts[checkout] = _Reading(mtime)
            return
        targets: set[Identity] = set()
        for link in links:
            try:
                target = link.resolve(strict=True)
            except FileNotFoundError:
                target = Path(os.path.realpath(link))
            except (OSError, RuntimeError) as exc:
                self.gaps.append(f"could not resolve the artifact link {link} ({exc})")
                continue
            targets |= self._lineage(target)
        self._checkouts[checkout] = _Reading(mtime, frozenset(targets))

    def _absence_unproven(self, checkout: Path) -> str:
        """Why an absent checkout may still exist elsewhere, or ``""`` when it is genuinely gone."""
        if self._locked is None:
            self._locked = _locked_worktrees(tuple(self._registries))
        if checkout in self._locked:
            return "its worktree is locked, so it lives on a volume this venue does not see"
        nearest = next((ancestor for ancestor in checkout.parents if ancestor.exists()), Path(checkout.anchor))
        # Absence is only proof inside a tree this venue walked or read; elsewhere the same path may be another volume.
        within_sight = any(
            ancestor in self._listed or self._read_here(ancestor) for ancestor in (nearest, *nearest.parents)
        )
        if within_sight and os.access(nearest, os.R_OK | os.X_OK):
            return ""
        return f"its parent {checkout.parent} cannot be observed from this venue"

    def _read_here(self, path: Path) -> bool:
        return (reading := self._checkouts.get(path)) is not None and reading.mtime_ns is not None

    def _lineage(self, target: Path) -> frozenset[Identity]:
        return frozenset(identity for path in (target, *target.parents) if (identity := self._identity(path)))

    def _identity(self, path: Path) -> Identity | None:
        if path not in self._identities:
            try:
                status = path.stat()
            except OSError:
                self._identities[path] = None
            else:
                self._identities[path] = (status.st_dev, status.st_ino)
        return self._identities[path]


def _locked_worktrees(clones: tuple[Path, ...]) -> frozenset[Path]:
    """Worktrees a clone's admin area marks ``locked`` — read from the files, no git call."""
    locked: set[Path] = set()
    for clone in clones:
        try:
            admin = list((clone / ".git" / "worktrees").iterdir())
        except OSError:
            continue
        locked.update(
            checkout
            for entry in admin
            if (entry / "locked").exists() and (checkout := checkout_of_admin_entry(entry)) is not None
        )
    return frozenset(locked)


def _mtime_ns(path: Path) -> int | None:
    try:
        return path.lstat().st_mtime_ns
    except OSError:
        return None


__all__ = ["ArtifactProtection"]
