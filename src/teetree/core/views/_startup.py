"""Shared startup/sync logic called by both dashboard init and sync-now button."""

import json

from teetree.config import DATA_DIR
from teetree.core.overlay_loader import get_overlay
from teetree.core.sync import SyncResult, sync_followup


def perform_sync() -> SyncResult:
    """Run followup sync and refresh caches.

    Called by:
    - Dashboard startup (DashboardView.get, first request)
    - "Sync now" button (SyncFollowupView.post)
    - CLI (t3 config write-skill-cache, for the cache part)

    Add any new sync-time work here so all entry points stay in sync.
    """
    result = sync_followup()
    _write_skill_metadata_cache()
    return result


def _write_skill_metadata_cache() -> None:
    """Write the active overlay's skill metadata to the XDG data directory.

    The UserPromptSubmit hook reads this cache to resolve companion skills
    without needing Django at hook time.
    """
    metadata = get_overlay().get_skill_metadata()
    cache_path = DATA_DIR / "skill-metadata.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
