"""Parse ``triggers:`` blocks from SKILL.md YAML frontmatter.

Standalone module with no Django or teatree imports — safe to use from both
the UserPromptSubmit hook (``skill_loader.py``) and the skill metadata
cache builder (``teatree.core.skill_cache``).
"""

from __future__ import annotations  # noqa: TID251 — standalone script, no teatree package imports

import contextlib
from dataclasses import dataclass, field

DEFAULT_PRIORITY = 50


@dataclass
class _ParserState:
    in_triggers: bool = False
    in_list_key: str = ""
    current_key: str = ""
    found: bool = False
    triggers: dict = field(
        default_factory=lambda: {
            "priority": DEFAULT_PRIORITY,
            "keywords": [],
            "urls": [],
            "exclude": "",
            "end_of_session": False,
            "search_hints": [],
            "requires": [],
            "companions": [],
        }
    )


def _extract_frontmatter(text: str) -> str | None:
    if not text.startswith("---"):
        return None
    try:
        end = text.index("---", 3)
    except ValueError:
        return None
    return text[3:end]


def _handle_top_level_key(key: str, state: _ParserState) -> bool:
    """Handle a top-level YAML key. Returns True if the line was consumed."""
    if key == "triggers":
        state.in_triggers = True
        state.in_list_key = ""
        state.current_key = ""
        state.found = True
        return True
    if key in {"search_hints", "requires", "companions"}:
        state.in_list_key = key
        state.in_triggers = False
        state.found = True
        return True
    state.in_triggers = False
    state.in_list_key = ""
    return True


def _collect_list_item(stripped: str, state: _ParserState) -> None:
    """Append a ``- value`` item to the active list key."""
    if stripped.startswith("- "):
        state.triggers[state.in_list_key].append(stripped.removeprefix("- ").strip().strip("'\""))


def _parse_trigger_line(stripped: str, triggers: dict, current_key: str) -> str:
    """Parse a single line inside the ``triggers:`` block."""
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


def parse_triggers(skill_md_text: str) -> dict | None:
    """Extract ``triggers:``, ``search_hints:``, ``requires:``, and ``companions:`` from SKILL.md frontmatter.

    Returns a dict with keys ``priority``, ``keywords``, ``urls``,
    ``exclude``, ``end_of_session``, ``search_hints``, ``requires``,
    ``companions`` — or ``None`` if none of these top-level fields is defined.
    """
    frontmatter = _extract_frontmatter(skill_md_text)
    if frontmatter is None:
        return None

    state = _ParserState()

    for line in frontmatter.splitlines():
        stripped = line.strip()

        if not line.startswith((" ", "\t")) and ":" in stripped:
            _handle_top_level_key(stripped.split(":")[0].strip(), state)
            continue

        if state.in_list_key:
            _collect_list_item(stripped, state)
            continue

        if state.in_triggers:
            state.current_key = _parse_trigger_line(stripped, state.triggers, state.current_key)

    return state.triggers if state.found else None
