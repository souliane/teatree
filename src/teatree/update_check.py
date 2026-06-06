"""Release-update check — split out of ``teatree.config`` for module-health LOC.

Polls the GitHub releases API for the latest teatree tag and caches the
verdict for 24h under ``DATA_DIR / "update-check.json"``. Consumed by the
CLI entry points to show a "new version available" notice without
blocking startup.

This module is config-free by design (no ``teatree.config`` import) to
avoid a cycle. The public entry point ``check_for_updates`` lives on
``teatree.config`` and reads the ``check_updates`` flag itself, then
delegates here with ``check_updates`` already resolved.
"""

import importlib.metadata
import json
import time
from pathlib import Path

from teatree.paths import DATA_DIR
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail


def run_update_check(*, check_updates: bool, force: bool = False) -> str | None:
    """Resolve a "new release available" notice; uses a 24h JSON cache.

    *check_updates* is the user's opt-in flag (from
    :class:`teatree.config.UserSettings`); the caller resolves it from
    config and passes it in so this module stays config-free. *force*
    bypasses both the opt-out and the cache (used by ``t3 config
    check-update`` to refresh on demand).
    """
    if not force and not check_updates:
        return None

    cache_path = DATA_DIR / "update-check.json"
    ttl = 86_400  # 24h

    # Return cached result when still fresh.
    if not force and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if time.time() - cached.get("ts", 0) < ttl:
                return cached.get("message") or None
        except (json.JSONDecodeError, OSError):
            pass

    current = importlib.metadata.version("teatree")

    try:
        result = run_allowed_to_fail(
            ["gh", "api", "repos/souliane/teatree/releases/latest", "--jq", ".tag_name"],
            expected_codes=None,
            timeout=10,
        )
        tag = result.stdout.strip()
    except (TimeoutExpired, FileNotFoundError):
        return None

    if not tag:
        return None

    latest = tag.lstrip("v")
    if latest == current:
        _write_update_cache(cache_path, "")
        return None

    message = f"teatree {tag} available (you have {current}). Run: uv pip install --upgrade teatree"
    _write_update_cache(cache_path, message)
    return message


def _write_update_cache(cache_path: Path, message: str) -> None:
    """Persist the update-check result so we don't hit the network every invocation."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"ts": time.time(), "message": message}),
        encoding="utf-8",
    )
