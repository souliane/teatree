"""Durable open-PR observations shared by the tick and dashboard."""

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

from django.utils import timezone

from teatree.core.models.pull_request import PullRequest
from teatree.paths import data_dir_root

_MINUTES_PER_HOUR = 60
_HOURS_PER_DAY = 24
_UNKNOWN_TIME = datetime.max.replace(tzinfo=UTC)


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
class OpenPrRow:
    ref: str
    url: str
    age: str
    next_task: str
    started: bool


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


def merged_entries(snapshots: Iterable[PrInventorySnapshot]) -> dict[str, PrInventoryEntry]:
    ordered = sorted(snapshots, key=lambda item: (not item.overlay.startswith("own:"), item.observed_at))
    return {entry.url: entry for snapshot in ordered for entry in snapshot.entries}


def parse_time(value: str | datetime | None) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def elapsed_label(value: str | datetime | None, now: datetime) -> str:
    at = parse_time(value)
    if at is None:
        return "unknown"
    minutes = max(0, int((now - at).total_seconds() // 60))
    if minutes < 1:
        return "< 1 min"
    if minutes < _MINUTES_PER_HOUR:
        return f"{minutes} min"
    hours = minutes // _MINUTES_PER_HOUR
    if hours < _HOURS_PER_DAY:
        return f"{hours} h"
    return f"{hours // _HOURS_PER_DAY} d"


def open_pr_rows(started_at: datetime, *, now: datetime | None = None) -> list[OpenPrRow]:
    """Every open PR observed since *started_at*: PRs with lifecycle rows first, then oldest first."""
    entries = merged_entries(snapshot for snapshot in read_inventory() if snapshot.observed_at >= started_at)
    started = set(PullRequest.objects.filter(url__in=list(entries)).values_list("url", flat=True))
    ordered = sorted(
        entries.values(),
        key=lambda entry: (
            entry.url not in started,
            parse_time(entry.created_at) or _UNKNOWN_TIME,
            entry.repo,
            entry.iid,
        ),
    )
    moment = now or timezone.now()
    return [
        OpenPrRow(
            ref=f"{entry.repo}!{entry.iid}",
            url=entry.url,
            age=elapsed_label(entry.created_at, moment),
            next_task=entry.next_task,
            started=entry.url in started,
        )
        for entry in ordered
    ]
