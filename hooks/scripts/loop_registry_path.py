"""Shared Django-free path, owner key and read of the hook tier's loop registry."""

import json
import os
from pathlib import Path

# The single registry key naming which *session* holds the host's attended loop slot.
OWNER_LOOP = "t3-loop-tick-owner"


def loop_registry_path() -> Path:
    override = os.environ.get("T3_LOOP_REGISTRY_DIR", "")
    base = (
        Path(override)
        if override
        else Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "teatree"
    )
    return base / "loop-registry.json"


def read_loop_registry() -> dict[str, dict]:
    path = loop_registry_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}
