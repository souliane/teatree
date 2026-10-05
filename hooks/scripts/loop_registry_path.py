"""Shared Django-free path, owner key and read of the hook tier's loop registry."""

import json
import os
from pathlib import Path

# #786 WS3: the immortal-roster name tuple (t3-main/review/cross-review/
# bug-hunt) is RETIRED — there is no fixed set of long-lived loop
# sub-agents. ``OWNER_LOOP`` remains only as the single registry key
# identifying which *session* is the tick-owner (the Django-free anchor
# the #758/#810 Stop self-pump gates on).
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
