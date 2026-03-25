"""Shared startup/sync logic called by both dashboard init and sync-now button."""

import contextlib
import json
from pathlib import Path

from teetree.config import DATA_DIR
from teetree.core.overlay_loader import get_overlay
from teetree.core.sync import SyncResult, sync_followup

# Default skill directory where Claude Code discovers skills.
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

    The UserPromptSubmit hook reads this cache to resolve companion skills
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


def _parse_triggers(skill_md_text: str) -> dict | None:
    """Extract the ``triggers:`` block from SKILL.md YAML frontmatter.

    Mirrors ``skill_loader.parse_triggers_from_frontmatter`` but lives in
    the Django package so the startup cache builder has no dependency on
    ``scripts/lib/``.
    """
    if not skill_md_text.startswith("---"):
        return None
    try:
        end = skill_md_text.index("---", 3)
    except ValueError:
        return None

    frontmatter = skill_md_text[3:end]
    in_triggers = False
    current_key = ""
    triggers: dict = {
        "priority": 50,
        "keywords": [],
        "urls": [],
        "exclude": "",
        "end_of_session": False,
    }
    found = False

    for line in frontmatter.splitlines():
        stripped = line.strip()

        if not line.startswith((" ", "\t")) and ":" in stripped:
            key = stripped.split(":")[0].strip()
            if key == "triggers":
                in_triggers = True
                found = True
                current_key = ""
                continue
            if in_triggers:
                break
            continue

        if not in_triggers:
            continue

        current_key = _parse_trigger_line(stripped, triggers, current_key)

    return triggers if found else None


def _parse_trigger_line(stripped: str, triggers: dict, current_key: str) -> str:
    """Parse a single line inside the ``triggers:`` block, returning the updated current_key."""
    if stripped.startswith("priority:"):
        with contextlib.suppress(ValueError, IndexError):
            triggers["priority"] = int(stripped.split(":", 1)[1].strip())
        return ""
    if stripped.startswith("exclude:"):
        triggers["exclude"] = stripped.split(":", 1)[1].strip().strip("'\"")
        return ""
    if stripped.startswith("end_of_session:"):
        val = stripped.split(":", 1)[1].strip().lower()
        triggers["end_of_session"] = val in {"true", "yes", "1"}
        return ""
    if stripped.startswith("keywords:"):
        return "keywords"
    if stripped.startswith("urls:"):
        return "urls"
    if stripped.startswith("- ") and current_key in {"keywords", "urls"}:
        triggers[current_key].append(stripped.removeprefix("- ").strip().strip("'\""))
    return current_key
