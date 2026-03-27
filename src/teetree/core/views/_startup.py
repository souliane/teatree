"""Shared startup/sync logic called by both dashboard init and sync-now button."""

import json
import sys
from pathlib import Path

from teetree.config import DATA_DIR
from teetree.core.overlay_loader import get_overlay
from teetree.core.sync import SyncResult, sync_followup

# Allow importing the shared trigger parser from scripts/lib/.
_SCRIPTS_LIB = Path(__file__).resolve().parents[4] / "scripts" / "lib"
if str(_SCRIPTS_LIB) not in sys.path:  # pragma: no branch
    sys.path.insert(0, str(_SCRIPTS_LIB))

from trigger_parser import parse_triggers as _parse_triggers  # noqa: E402  # ty: ignore[unresolved-import]

# Default skill directory where Claude Code discovers skills.
# When supporting other agent platforms, make this configurable via settings.
_CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"


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

    The UserPromptSubmit hook reads this cache to resolve overlay matching
    and the trigger index without needing Django at hook time.
    """
    metadata = get_overlay().get_skill_metadata()
    metadata["trigger_index"] = _build_trigger_index()
    cache_path = DATA_DIR / "skill-metadata.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def _build_trigger_index() -> list[dict]:
    """Scan ``~/.claude/skills/*/SKILL.md`` and extract ``triggers:`` blocks.

    Returns a list of trigger entries sorted by priority, each with keys:
    ``skill``, ``priority``, ``keywords``, ``urls``, ``exclude``,
    ``end_of_session``.
    """
    index: list[dict] = []

    if not _CLAUDE_SKILLS_DIR.is_dir():
        return index

    for skill_dir in sorted(_CLAUDE_SKILLS_DIR.iterdir()):
        # Resolve symlinks so we can check if target exists
        resolved = skill_dir.resolve() if skill_dir.is_symlink() else skill_dir
        if not resolved.is_dir():
            continue
        skill_md = resolved / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
        except OSError:
            continue
        triggers = _parse_triggers(text)
        if triggers is None:
            continue
        index.append({"skill": skill_dir.name, **triggers})

    import operator  # noqa: PLC0415

    index.sort(key=operator.itemgetter("priority"))
    return index


# _parse_triggers is imported from scripts/lib/trigger_parser.py (single source of truth).
