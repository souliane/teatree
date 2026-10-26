"""Inventory password-store entries and copy selected rows on request."""

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from django.db import OperationalError

from teatree.core.sealed.references import referenced_entry_names
from teatree.utils.secret_backups import backup_base, expired_backup_names, is_backup
from teatree.utils.secrets import PassStore, SecretStore, SecretStoreError

_PROBE = "teatree/slack-app-config-issued-at-writability-probe"
_MANAGED_PREFIXES = ("teatree/github-webhook/", "anthropic/")


@dataclass(frozen=True, slots=True)
class ImportEntry:
    name: str
    destination: str
    readability: str


class PasswordStoreDirectoryError(FileNotFoundError):
    def __init__(self, root: Path) -> None:
        super().__init__(f"password store directory is unreadable: {root}")


def _present_names(root: Path) -> frozenset[str]:
    store_root = root.resolve()
    if not store_root.is_dir():
        raise PasswordStoreDirectoryError(root)
    names: set[str] = set()

    def raise_walk_error(error: OSError) -> None:
        raise PasswordStoreDirectoryError(Path(error.filename or root)) from error

    for directory, _, files in os.walk(store_root, followlinks=False, onerror=raise_walk_error):
        for filename in files:
            path = Path(directory) / filename
            if filename.endswith(".gpg") and path.is_file() and not path.is_symlink():
                names.add(path.relative_to(store_root).as_posix().removesuffix(".gpg"))
    return frozenset(names)


def _readability(store: SecretStore, name: str) -> str:
    try:
        return "readable" if store.read(name) is not None else "missing"
    except (SecretStoreError, FileNotFoundError):
        return "unreadable"


def build_import_plan(
    *, store: SecretStore | None = None, root: Path | None = None, check_readability: bool = True
) -> tuple[ImportEntry, ...]:
    source = store if store is not None else PassStore()
    store_root = (
        root if root is not None else Path(os.environ.get("PASSWORD_STORE_DIR") or Path.home() / ".password-store")
    )
    referenced = referenced_entry_names()
    present = _present_names(store_root)
    entries: list[ImportEntry] = []
    for name in sorted(present | referenced):
        if name == _PROBE:
            destination = "drop"
        elif (
            name in referenced
            or name.startswith(_MANAGED_PREFIXES)
            or (is_backup(name) and backup_base(name) in referenced)
        ):
            destination = "row" if name in present else "missing"
        else:
            destination = "external"
        readability = (
            _readability(source, name)
            if name in present and destination == "row" and check_readability
            else "not-checked"
            if name in present
            else "missing"
        )
        entries.append(ImportEntry(name, destination, readability))
    return tuple(entries)


@dataclass(frozen=True, slots=True)
class CopyOutcome:
    status: str
    name: str


def _copy_one(source: SecretStore, target: SecretStore, name: str) -> CopyOutcome:
    try:
        value = source.read(name)
        if value is None:
            return CopyOutcome("failed", name)
        if not value.strip():
            return CopyOutcome("empty", name)
        existing = target.read(name)
        if existing == value:
            return CopyOutcome("unchanged", name)
        target.write(name, value)
        status = "copied" if existing is None else "updated"
        return CopyOutcome(status if target.read(name) == value else "failed", name)
    except (SecretStoreError, FileNotFoundError, OperationalError, ValueError):
        return CopyOutcome("failed", name)


def _verified(source: SecretStore, target: SecretStore, name: str) -> bool:
    try:
        return target.read(name) == source.read(name)
    except (SecretStoreError, FileNotFoundError, OperationalError, ValueError):
        return False


def apply_import(
    source: SecretStore, target: SecretStore, plan: tuple[ImportEntry, ...], *, now: datetime
) -> tuple[CopyOutcome, ...]:
    """Copy every ``row`` entry from *source* to *target*; every outcome is a name and a status, never a value.

    Backups beyond the retention window are ``expired``, not failed, so the store's own pruning cannot turn a
    copy into a read-back mismatch. A second full read-back runs after the last write, so a rotating pair that
    changed mid-run shows as ``failed`` instead of being reported copied.
    """
    rows = [entry.name for entry in plan if entry.destination == "row" and entry.readability != "missing"]
    backup_rows = [name for name in rows if is_backup(name)]
    existing_backups: set[str] = set()
    for base in {backup_base(name) for name in backup_rows}:
        parent, _, _ = base.rpartition("/")
        stem = f"{base}.bak-"
        try:
            existing_backups.update(
                f"{parent}/{name}" if parent else name
                for name in target.names(parent)
                if (f"{parent}/{name}" if parent else name).startswith(stem)
            )
        except (SecretStoreError, OperationalError):
            return tuple(CopyOutcome("failed", name) for name in rows)
    expired = expired_backup_names((*backup_rows, *existing_backups), now)
    outcomes = [CopyOutcome("expired", name) if name in expired else _copy_one(source, target, name) for name in rows]
    return tuple(
        CopyOutcome("failed", outcome.name)
        if outcome.status in {"copied", "updated", "unchanged"} and not _verified(source, target, outcome.name)
        else outcome
        for outcome in outcomes
    )
