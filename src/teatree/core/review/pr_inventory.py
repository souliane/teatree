"""Durable open-PR observations shared by the tick and dashboard."""

import json
import logging
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

from django.utils import timezone

from teatree.core.models.pull_request import PullRequest
from teatree.core.overlay_loader import OverlayConfigResolver
from teatree.core.review.pr_next_action import stall_cause
from teatree.paths import data_dir_root

MINUTES_PER_HOUR = 60
HOURS_PER_DAY = 24
_UNKNOWN_TIME = datetime.max.replace(tzinfo=UTC)
logger = logging.getLogger(__name__)


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
    head_sha: str = ""
    approvals_display: str = ""
    author: str = ""
    owned: bool | None = None


@dataclass(frozen=True, slots=True)
class OpenPrRow:
    ref: str
    url: str
    age: str
    next_task: str
    started: bool
    stall: str
    head_sha: str = ""
    decision: str = ""


@dataclass(frozen=True, slots=True)
class PrInventorySnapshot:
    overlay: str
    observed_at: datetime
    entries: tuple[PrInventoryEntry, ...]
    watched_repos: tuple[str, ...] = ()
    verified: bool = True


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
    watched_repos: tuple[str, ...] = (),
) -> Path:
    snapshot = PrInventorySnapshot(
        overlay=overlay,
        observed_at=observed_at or timezone.now(),
        entries=tuple(entries),
        watched_repos=watched_repos,
    )
    return _write_snapshot(snapshot, directory)


def _write_snapshot(snapshot: PrInventorySnapshot, directory: Path | None) -> Path:
    root = _directory(directory)
    root.mkdir(parents=True, exist_ok=True)
    target = _path(snapshot.overlay, root)
    payload = {
        "overlay": snapshot.overlay,
        "observed_at": snapshot.observed_at.isoformat(),
        "entries": [asdict(entry) for entry in snapshot.entries],
        "watched_repos": list(snapshot.watched_repos),
        "verified": snapshot.verified,
    }
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, prefix=".pr-inventory-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream)
            stream.write("\n")
        if _read_snapshot(temporary) != snapshot:
            message = f"PR inventory write did not verify: {temporary}"
            raise PrInventoryError(message)
        temporary.replace(target)
        if snapshot.overlay.startswith("own:"):
            prefix = snapshot.overlay.rsplit(":", 1)[0] + ":"
            for old_path in root.glob("*.json"):
                if old_path == target:
                    continue
                try:
                    old = _read_snapshot(old_path)
                except PrInventoryError:
                    continue
                if old.overlay.startswith(prefix):
                    old_path.unlink(missing_ok=True)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def mark_inventory_unknown(overlay: str, *, directory: Path | None = None) -> Path:
    path = _path(overlay, _directory(directory))
    try:
        prior = _read_snapshot(path) if path.exists() else None
    except PrInventoryError:
        logger.warning("Replacing unreadable PR inventory before invalidation: %s", path)
        prior = None
    return _write_snapshot(
        PrInventorySnapshot(
            overlay=overlay,
            observed_at=prior.observed_at if prior else timezone.now(),
            entries=prior.entries if prior else (),
            watched_repos=prior.watched_repos if prior else (),
            verified=False,
        ),
        directory,
    )


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
        watched_repos = tuple(payload.get("watched_repos", ()))
        verified = payload.get("verified", True)
    except (TypeError, KeyError, ValueError) as exc:
        message = f"Unreadable PR inventory: {path}"
        raise PrInventoryError(message) from exc
    if observed_at.tzinfo is None:
        message = f"PR inventory time has no timezone: {path}"
        raise PrInventoryError(message)
    invalid_identity = not isinstance(overlay, str) or not overlay
    invalid_metadata = (
        not isinstance(payload.get("watched_repos", ()), list | tuple)
        or any(not isinstance(repo, str) for repo in watched_repos)
        or not isinstance(verified, bool)
    )
    invalid_entry = any(
        (value is not None and type(value) is not bool)
        if name == "owned"
        else type(value) is not (int if name == "iid" else bool if name == "draft" else str)
        for entry in entries
        for name, value in asdict(entry).items()
    )
    if invalid_identity or invalid_metadata or invalid_entry:
        message = f"Unreadable PR inventory: {path}"
        raise PrInventoryError(message)
    return PrInventorySnapshot(
        overlay=overlay, observed_at=observed_at, entries=entries, watched_repos=watched_repos, verified=verified
    )


def read_inventory(
    *, directory: Path | None = None, errors: list[str] | None = None, active_overlays: Iterable[str] | None = None
) -> tuple[PrInventorySnapshot, ...]:
    root = _directory(directory)
    if not root.exists():
        return ()
    snapshots = []
    failures = []
    active = set(active_overlays) if active_overlays is not None else None
    for path in root.glob("*.json"):
        try:
            snapshot = _read_snapshot(path)
        except PrInventoryError as exc:
            failures.append(str(exc))
            logger.warning("%s", exc)
            continue
        overlay = snapshot.overlay
        if overlay.startswith("own:"):
            overlay = overlay.rsplit(":", 1)[0].removeprefix("own:")
        if active is not None and overlay and overlay not in active:
            path.unlink(missing_ok=True)
            logger.info("Pruned PR inventory for removed overlay %s", overlay)
            continue
        snapshots.append(snapshot)
    if errors is not None:
        errors.extend(failures)
    if failures and not snapshots:
        raise PrInventoryError("; ".join(failures))
    return tuple(sorted(snapshots, key=lambda row: row.overlay))


def merged_entries(snapshots: Iterable[PrInventorySnapshot]) -> dict[str, PrInventoryEntry]:
    ordered = sorted(snapshots, key=lambda item: (not item.overlay.startswith("own:"), item.observed_at))
    owned: dict[str, tuple[PrInventoryEntry, datetime]] = {}
    merged: dict[str, PrInventoryEntry] = {}
    for snapshot in ordered:
        for entry in snapshot.entries:
            resolved = entry
            if snapshot.overlay.startswith("own:"):
                owned[entry.url] = (entry, snapshot.observed_at)
            elif (
                (own_record := owned.get(entry.url)) is not None
                and own_record[0].head_sha
                and own_record[0].head_sha == entry.head_sha
            ):
                own, own_at = own_record
                preferred = own if own_at > snapshot.observed_at else entry
                resolved = replace(
                    entry,
                    ci=preferred.ci,
                    approvals=preferred.approvals,
                    approvals_display=preferred.approvals_display,
                    threads=preferred.threads,
                    author=own.author or entry.author,
                    owned=True,
                )
            merged[entry.url] = resolved
    return merged


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
    if minutes < MINUTES_PER_HOUR:
        return f"{minutes} min"
    hours = minutes // MINUTES_PER_HOUR
    if hours < HOURS_PER_DAY:
        return f"{hours} h"
    return f"{hours // HOURS_PER_DAY} d"


def open_pr_rows(
    started_at: datetime, *, now: datetime | None = None, errors: list[str] | None = None
) -> list[OpenPrRow]:
    """Every open PR observed since *started_at*: PRs with lifecycle rows first, then oldest first."""
    entries = merged_entries(
        snapshot
        for snapshot in read_inventory(errors=errors, active_overlays=OverlayConfigResolver.all_names())
        if snapshot.observed_at >= started_at
    )
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
            stall=stall_cause(entry),
            head_sha=entry.head_sha,
            decision=entry.decision,
        )
        for entry in ordered
    ]
