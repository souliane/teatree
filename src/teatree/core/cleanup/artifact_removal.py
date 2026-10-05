"""Deletion anchored to the checkout and artifact identities that authorised it.

The artifact is renamed aside first, so it leaves its path atomically: a deletion the pass
budget interrupts leaves a ``.t3-evicted-*`` tree the next pass finishes, never a
half-deleted ``.venv`` that tools mistake for a working one and dormancy keeps for days.
"""

import os
import time
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

type FileIdentity = tuple[int, int, int]

EVICTED_PREFIX = ".t3-evicted-"

_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True, slots=True)
class AnchoredArtifact:
    """An artifact and the identities that authorised removing it."""

    artifact: Path
    checkout: Path
    artifact_identity: FileIdentity | None
    checkout_identity: FileIdentity | None
    rebuild_inputs: tuple[str, ...] | None


def remove_anchored_artifact(target: AnchoredArtifact, *, deadline: float | None = None) -> str:
    """Remove *target* if it is still the directory that was authorised; ``""`` or why not."""
    aside, reason = _move_aside(target)
    if reason:
        return reason
    return _clear_aside(target.checkout, aside, deadline=deadline)


def _move_aside(target: AnchoredArtifact) -> tuple[str, str]:
    checkout_descriptor = -1
    artifact_descriptor = -1
    name = target.artifact.name
    try:
        checkout_descriptor = os.open(target.checkout, _DIRECTORY_OPEN_FLAGS)
        artifact_descriptor = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=checkout_descriptor)
        if reason := _authorisation_lapsed(checkout_descriptor, artifact_descriptor, target):
            return "", reason
        aside = f"{EVICTED_PREFIX}{name}-{time.time_ns()}"
        os.rename(name, aside, src_dir_fd=checkout_descriptor, dst_dir_fd=checkout_descriptor)
        if _entry_identity(checkout_descriptor, aside) != target.artifact_identity:
            os.rename(aside, name, src_dir_fd=checkout_descriptor, dst_dir_fd=checkout_descriptor)
            return "", "the artifact identity changed during deletion; the replacement was put back"
    except OSError as exc:
        return "", f"{_failed_delete_state(target.artifact)} — {exc}"
    finally:
        if artifact_descriptor >= 0:
            os.close(artifact_descriptor)
        if checkout_descriptor >= 0:
            os.close(checkout_descriptor)
    return aside, ""


def _authorisation_lapsed(checkout_descriptor: int, artifact_descriptor: int, target: AnchoredArtifact) -> str:
    if _descriptor_identity(checkout_descriptor) != target.checkout_identity:
        return "the checkout identity changed before deletion"
    if _descriptor_identity(artifact_descriptor) != target.artifact_identity:
        return "the artifact identity changed before deletion"
    if reason := _unrebuildable_reason(checkout_descriptor, target.rebuild_inputs):
        return reason
    if _entry_identity(checkout_descriptor, target.artifact.name) != target.artifact_identity:
        return "the artifact identity changed before deletion"
    return ""


def clear_evicted(checkout: Path, name: str, *, deadline: float | None) -> tuple[bool, int]:
    """Finish removing an aside tree an earlier pass left; ``(complete, bytes freed)``."""
    descriptor = os.open(checkout, _DIRECTORY_OPEN_FLAGS)
    try:
        return _clear_tree(descriptor, name, deadline=deadline)
    finally:
        os.close(descriptor)


def _clear_aside(checkout: Path, aside: str, *, deadline: float | None) -> str:
    try:
        complete, _freed = clear_evicted(checkout, aside, deadline=deadline)
    except OSError as exc:
        return (
            f"the delete FAILED PART-WAY after moving it aside as {aside} — it must be rebuilt before use, "
            f"and the next pass clears the rest — {exc}"
        )
    if not complete:
        return f"the pass's time budget ran out mid-delete — moved aside as {aside}; the next pass clears the rest"
    return ""


def _clear_tree(parent: int, name: str, *, deadline: float | None) -> tuple[bool, int]:
    freed = 0
    descriptor = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent)
    try:
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if deadline is not None and time.monotonic() >= deadline:
                    return False, freed
                if entry.is_dir(follow_symlinks=False):
                    complete, nested = _clear_tree(descriptor, entry.name, deadline=deadline)
                    freed += nested
                    if not complete:
                        return False, freed
                else:
                    size = entry.stat(follow_symlinks=False).st_size
                    os.unlink(entry.name, dir_fd=descriptor)
                    freed += size
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=parent)
    return True, freed


def _descriptor_identity(descriptor: int) -> FileIdentity:
    value = os.fstat(descriptor)
    return value.st_dev, value.st_ino, value.st_mode


def _entry_identity(descriptor: int, name: str) -> FileIdentity | None:
    try:
        value = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except OSError:
        return None
    return value.st_dev, value.st_ino, value.st_mode


def _unrebuildable_reason(checkout_descriptor: int, inputs: tuple[str, ...] | None) -> str:
    if not inputs or any(_rebuild_input_present(checkout_descriptor, name) for name in inputs):
        return ""
    return f"nothing in the checkout rebuilds it — no {' / '.join(inputs)}, so the documented rebuild would refuse"


def _rebuild_input_present(checkout_descriptor: int, name: str) -> bool:
    parent, _, pattern = name.rpartition("/")
    descriptor = checkout_descriptor
    close_descriptor = False
    try:
        if parent:
            descriptor = os.open(parent, _DIRECTORY_OPEN_FLAGS, dir_fd=checkout_descriptor)
            close_descriptor = True
        if "*" not in pattern:
            os.stat(pattern, dir_fd=descriptor)
            return True
        with os.scandir(descriptor) as entries:
            return any(fnmatchcase(entry.name, pattern) and _entry_exists(descriptor, entry.name) for entry in entries)
    except OSError:
        return False
    finally:
        if close_descriptor:
            os.close(descriptor)


def _entry_exists(descriptor: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=descriptor)
    except OSError:
        return False
    return True


def _failed_delete_state(artifact: Path) -> str:
    if not artifact.exists():
        return "the delete raised after removing the tree"
    return "the delete FAILED PART-WAY — the artifact may be incomplete and must be rebuilt before use"


__all__ = ["EVICTED_PREFIX", "AnchoredArtifact", "clear_evicted", "remove_anchored_artifact"]
