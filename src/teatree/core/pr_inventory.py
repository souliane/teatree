"""Durable open-PR observations shared by the tick and dashboard."""

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

from django.utils import timezone

from teatree.paths import data_dir_root


class PrInventoryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PrInventoryEntry:
    url: str
    repo: str
    iid: int
    title: str
    created_at: str
    updated_at: str
    next_task: str
    decision: str
    ci: str
    approvals: str
    threads: str
    draft: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class PrInventorySnapshot:
    overlay: str
    observed_at: datetime
    entries: tuple[PrInventoryEntry, ...]


def _directory(directory: Path | None) -> Path:
    return directory if directory is not None else data_dir_root() / "pr-inventory"


def _path(overlay: str, directory: Path) -> Path:
    return directory / f"{sha256(overlay.encode()).hexdigest()}.json"


def write_inventory(
    overlay: str,
    entries: list[PrInventoryEntry],
    *,
    directory: Path | None = None,
    observed_at: datetime | None = None,
) -> Path:
    root = _directory(directory)
    root.mkdir(parents=True, exist_ok=True)
    target = _path(overlay, root)
    snapshot = PrInventorySnapshot(overlay=overlay, observed_at=observed_at or timezone.now(), entries=tuple(entries))
    payload = {
        "overlay": snapshot.overlay,
        "observed_at": snapshot.observed_at.isoformat(),
        "entries": [asdict(entry) for entry in snapshot.entries],
    }
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, prefix=".pr-inventory-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream)
            stream.write("\n")
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    if _read_snapshot(target) != snapshot:
        message = f"PR inventory write did not verify: {target}"
        raise PrInventoryError(message)
    return target


def _read_snapshot(path: Path) -> PrInventorySnapshot:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        message = f"Unreadable PR inventory: {path}"
        raise PrInventoryError(message) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        message = f"Unreadable PR inventory: {path}"
        raise PrInventoryError(message)
    try:
        entries = tuple(PrInventoryEntry(**row) for row in payload["entries"])
        observed_at = datetime.fromisoformat(payload["observed_at"])
        overlay = payload["overlay"]
    except (TypeError, KeyError, ValueError) as exc:
        message = f"Unreadable PR inventory: {path}"
        raise PrInventoryError(message) from exc
    if observed_at.tzinfo is None:
        message = f"PR inventory time has no timezone: {path}"
        raise PrInventoryError(message)
    if (
        not isinstance(overlay, str)
        or not overlay
        or any(
            type(value) is not (int if name == "iid" else bool if name == "draft" else str)
            for entry in entries
            for name, value in asdict(entry).items()
        )
    ):
        message = f"Unreadable PR inventory: {path}"
        raise PrInventoryError(message)
    return PrInventorySnapshot(overlay=overlay, observed_at=observed_at, entries=entries)


def read_inventory(*, directory: Path | None = None) -> tuple[PrInventorySnapshot, ...]:
    root = _directory(directory)
    if not root.exists():
        return ()
    return tuple(sorted((_read_snapshot(path) for path in root.glob("*.json")), key=lambda row: row.overlay))
