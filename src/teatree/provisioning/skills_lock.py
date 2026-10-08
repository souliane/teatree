"""The skills CLI's install record: the ref each skill was REQUESTED at, not the commit that was installed."""

import json
import os
from pathlib import Path

_SCHEMA = 3


def lock_path(home: Path) -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    return Path(state_home) / "skills" / ".skill-lock.json" if state_home else home / ".agents" / ".skill-lock.json"


def read_install_refs(home: Path) -> dict[str, tuple[str, str]] | None:
    """``{skill: (source, ref)}`` lowercased, or ``None`` when the record is absent, unparsable or another schema."""
    try:
        record = json.loads(lock_path(home).read_text(encoding="utf-8"))
        if record["version"] != _SCHEMA:
            return None
        refs: dict[str, tuple[str, str]] = {}
        for name, entry in record["skills"].items():
            source = entry.get("source")
            refs[name] = ((source if source is not None else "").lower(), (entry.get("ref") or "").lower())
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return refs
