"""Parse ``triggers:`` blocks from SKILL.md YAML frontmatter.

Standalone module with no Django or teatree imports — safe to use from both
the UserPromptSubmit hook (``skill_loader.py``) and the Django startup cache
builder (``teatree.core.views._startup``).
"""

from __future__ import annotations  # noqa: TID251 — standalone script, no teatree package imports

import contextlib

# Default priority when a skill has triggers but no explicit priority.
DEFAULT_PRIORITY = 50


def parse_triggers(skill_md_text: str) -> dict | None:
    """Extract the ``triggers:`` and ``search_hints:`` blocks from SKILL.md frontmatter.

    Returns a dict with keys ``priority``, ``keywords``, ``urls``,
    ``exclude``, ``end_of_session``, ``search_hints`` — or ``None``
    if neither ``triggers:`` nor ``search_hints:`` is defined.

    ``search_hints`` is a top-level frontmatter field (not nested under
    ``triggers:``).  It lists simple keywords for agent-launch skill
    discovery — conceptually different from ``triggers.keywords`` which
    are regex patterns for the UserPromptSubmit hook.
    """
    if not skill_md_text.startswith("---"):
        return None
    try:
        end = skill_md_text.index("---", 3)
    except ValueError:
        return None

    frontmatter = skill_md_text[3:end]

    in_triggers = False
    in_search_hints = False
    current_key = ""
    triggers: dict = {
        "priority": DEFAULT_PRIORITY,
        "keywords": [],
        "urls": [],
        "exclude": "",
        "end_of_session": False,
        "search_hints": [],
    }
    found = False

    for line in frontmatter.splitlines():
        stripped = line.strip()

        # Top-level key detection (not indented)
        if not line.startswith((" ", "\t")) and ":" in stripped:
            key = stripped.split(":")[0].strip()
            in_search_hints = False
            if key == "triggers":
                in_triggers = True
                found = True
                current_key = ""
                continue
            if key == "search_hints":
                in_search_hints = True
                found = True
                if in_triggers:
                    in_triggers = False
                continue
            if in_triggers:
                in_triggers = False
            continue

        if in_search_hints:
            if stripped.startswith("- "):
                triggers["search_hints"].append(stripped.removeprefix("- ").strip().strip("'\""))
            continue

        if not in_triggers:
            continue

        # Inside triggers block — parse nested keys and list items
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
