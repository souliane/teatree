"""Read a structured verdict from a command's mixed output."""

import contextlib
import json
from typing import Any, cast


def last_json_object(text: str) -> dict[str, Any] | None:
    for raw in reversed(text.strip().splitlines()):
        line = raw.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        with contextlib.suppress(json.JSONDecodeError):
            return cast("dict[str, Any]", json.loads(line))
    return None
