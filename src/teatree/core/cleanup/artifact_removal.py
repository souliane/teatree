"""Deletion anchored to the checkout and artifact identities that authorised it."""

import os
import shutil
from fnmatch import fnmatchcase
from pathlib import Path

type FileIdentity = tuple[int, int, int]

_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def remove_anchored_artifact(
    *,
    artifact: Path,
    checkout: Path,
    artifact_identity: FileIdentity | None,
    checkout_identity: FileIdentity | None,
    rebuild_inputs: tuple[str, ...] | None,
) -> str:
    checkout_descriptor = -1
    artifact_descriptor = -1
    try:
        checkout_descriptor = os.open(checkout, _DIRECTORY_OPEN_FLAGS)
        if _descriptor_identity(checkout_descriptor) != checkout_identity:
            return "the checkout identity changed before deletion"
        artifact_descriptor = os.open(
            artifact.name,
            _DIRECTORY_OPEN_FLAGS,
            dir_fd=checkout_descriptor,
        )
        if _descriptor_identity(artifact_descriptor) != artifact_identity:
            return "the artifact identity changed before deletion"
        if reason := _unrebuildable_reason(checkout_descriptor, rebuild_inputs):
            return reason
        _clear_directory(artifact_descriptor)
        if _entry_identity(checkout_descriptor, artifact.name) != artifact_identity:
            return "the artifact identity changed during deletion; the original may be incomplete"
        os.rmdir(artifact.name, dir_fd=checkout_descriptor)
    except OSError as exc:
        return f"{_failed_delete_state(artifact)} — {exc}"
    finally:
        if artifact_descriptor >= 0:
            os.close(artifact_descriptor)
        if checkout_descriptor >= 0:
            os.close(checkout_descriptor)
    return ""


def _descriptor_identity(descriptor: int) -> FileIdentity:
    value = os.fstat(descriptor)
    return value.st_dev, value.st_ino, value.st_mode


def _entry_identity(descriptor: int, name: str) -> FileIdentity | None:
    try:
        value = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except OSError:
        return None
    return value.st_dev, value.st_ino, value.st_mode


def _clear_directory(descriptor: int) -> None:
    with os.scandir(descriptor) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.name, dir_fd=descriptor)
            else:
                os.unlink(entry.name, dir_fd=descriptor)


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


__all__ = ["remove_anchored_artifact"]
