"""Atomic supervisor heartbeat shared with the private factory health listener."""

import os
import re
import tempfile
import time
from pathlib import Path

from teatree.paths import DATA_DIR

HEARTBEAT_NAME = "worker-health"
MAX_MARKER_BYTES = 512
_GENERATION = re.compile(r"[0-9a-f]{32}\Z")


def read_fields(path: Path) -> dict[str, str] | None:
    """Read a small, strict key/value marker; missing or partial input is unknown."""
    try:
        with path.open("rb") as stream:
            contents = stream.read(MAX_MARKER_BYTES + 1)
        if len(contents) > MAX_MARKER_BYTES:
            return None
        lines = contents.decode("ascii").splitlines()
    except (OSError, UnicodeError):
        return None
    fields: dict[str, str] = {}
    for line in lines:
        if "=" not in line:
            return None
        key, value = line.split("=", 1)
        if not key or key in fields:
            return None
        fields[key] = value
    return fields


def ready_generation(data_dir: Path) -> str | None:
    fields = read_fields(data_dir / "boot-latest")
    if fields is None or any(
        fields.get(key) != value
        for key, value in (("status", "ready"), ("stage", "ready"), ("cause", "ready"), ("missing", ""))
    ):
        return None
    generation = fields.get("generation", "")
    return generation if _GENERATION.fullmatch(generation) else None


def publish_worker_heartbeat(admission: str, *, active: bool, data_dir: Path | None = None) -> None:
    """Record a completed poll using the generation pinned at worker startup."""
    generation = os.environ.get("T3_WORKER_HEALTH_GENERATION", "")
    if not _GENERATION.fullmatch(generation):
        return
    if data_dir is None:
        data_dir = Path(os.environ.get("T3_WORKER_HEALTH_DIR", str(DATA_DIR)))
    payload = f"generation={generation}\nepoch={int(time.time())}\nadmission={admission}\nactive={int(active)}\n"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="ascii", dir=data_dir, prefix=".worker-health-", delete=False
        ) as stream:
            temp_path = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
        temp_path.replace(data_dir / HEARTBEAT_NAME)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
