"""Cross-venue health records for ORM-backed cold hooks."""

import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import TypedDict, cast

from teatree.hooks._hook_state import shared_hook_state_root


class OrmHealthRecord(TypedDict, total=False):
    status: str
    interpreter: str
    db_path: str
    venue: str
    cause: str
    at: float


MARKER_TTL_SECONDS = 24 * 60 * 60
_OK_HEARTBEAT_SECONDS = 60 * 60


def hook_venue() -> str:
    return "container" if Path("/.dockerenv").exists() else "host"


def health_path(*, interpreter: str | None = None, venue: str | None = None) -> Path:
    identity = f"{venue or hook_venue()}\0{interpreter or sys.executable}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return shared_hook_state_root() / f"hook-orm-health-{digest}.json"


def record_orm_health(status: str, db_path: str, cause: str = "") -> bool:
    path = health_path()
    now = time.time()
    try:
        if status == "ok" and path.is_file() and now - path.stat().st_mtime < _OK_HEARTBEAT_SECONDS:
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                previous = {}
            if isinstance(previous, dict) and previous.get("status") == "ok":
                return True
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "status": status,
            "interpreter": sys.executable,
            "db_path": db_path,
            "venue": hook_venue(),
            "cause": cause,
            "at": now,
        }
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temp:
            json.dump(record, temp)
            temp_path = Path(temp.name)
        try:
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        return False
    return True


def read_orm_health(*, now: float | None = None) -> list[OrmHealthRecord]:
    moment = time.time() if now is None else now
    records: list[OrmHealthRecord] = []
    for path in shared_hook_state_root().glob("hook-orm-health-*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or not isinstance(record.get("at"), (int, float)):
                records.append({"status": "error", "interpreter": str(path), "cause": "unreadable health record"})
                continue
            if moment - record["at"] <= MARKER_TTL_SECONDS:
                records.append(cast("OrmHealthRecord", record))
        except (OSError, ValueError, TypeError):
            records.append({"status": "error", "interpreter": str(path), "cause": "unreadable health record"})
    return records
