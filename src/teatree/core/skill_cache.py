"""Skill metadata cache.

Writes the active overlay's skill metadata + skill (requires) index to
``$DATA_DIR/skill-metadata.json``. The UserPromptSubmit hook reads the
cache to resolve overlay matching and the requires closure without paying
the cost of Django bootstrap on every prompt.

Called from `t3 config write-skill-cache` and from the loop tick
when its scanners notice a SKILL.md mtime change. The dashboard's
"sync now" entry point that previously called this is gone in #541.
"""

import json
import logging
import operator
from pathlib import Path

import teatree
from teatree.core.overlay_loader import get_overlay
from teatree.paths import DATA_DIR
from teatree.skill_support.deps import resolve_all
from teatree.skill_support.requires_parser import parse_requires
from teatree.skill_support.schema import validate_skill_md

logger = logging.getLogger(__name__)

# Default skill directory where Claude Code discovers skills.
_CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"


def write_skill_metadata_cache() -> None:
    """Write the active overlay's skill metadata to the XDG data directory."""
    metadata = get_overlay().metadata.get_skill_metadata()
    skill_index = _build_requires_index()
    metadata["skill_index"] = skill_index
    metadata["resolved_requires"] = resolve_all(skill_index)
    metadata["skill_mtimes"] = _collect_skill_mtimes()
    metadata["teatree_version"] = teatree.__version__
    cache_path = DATA_DIR / "skill-metadata.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def _validate_skills(known_skills: set[str]) -> None:
    if not _CLAUDE_SKILLS_DIR.is_dir():
        return
    for d in sorted(_CLAUDE_SKILLS_DIR.iterdir()):
        resolved = d.resolve() if d.is_symlink() else d
        if not resolved.is_dir():
            continue
        skill_md = resolved / "SKILL.md"
        if not skill_md.is_file():
            continue
        errors, warnings = validate_skill_md(skill_md, known_skills=known_skills)
        for warning in warnings:
            logger.warning("%s", warning)
        for error in errors:
            logger.warning("Skill validation error: %s", error)


def _build_requires_index() -> list[dict]:
    """Scan ``~/.claude/skills/*/SKILL.md`` and index each skill's ``requires:``."""
    index: list[dict] = []

    if not _CLAUDE_SKILLS_DIR.is_dir():
        return index

    known_skills: set[str] = set()
    for d in _CLAUDE_SKILLS_DIR.iterdir():
        resolved = d.resolve() if d.is_symlink() else d
        if resolved.is_dir() and (resolved / "SKILL.md").is_file():
            known_skills.add(d.name)

    _validate_skills(known_skills)

    for skill_dir in sorted(_CLAUDE_SKILLS_DIR.iterdir()):
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
        requires = parse_requires(text)
        index.append({"skill": skill_dir.name, "requires": requires or []})

    index.sort(key=operator.itemgetter("skill"))
    return index


def _collect_skill_mtimes() -> dict[str, int]:
    """Collect mtime_ns for each SKILL.md file in the skills directory."""
    mtimes: dict[str, int] = {}
    if not _CLAUDE_SKILLS_DIR.is_dir():
        return mtimes
    for skill_dir in _CLAUDE_SKILLS_DIR.iterdir():
        resolved = skill_dir.resolve() if skill_dir.is_symlink() else skill_dir
        if not resolved.is_dir():
            continue
        skill_md = resolved / "SKILL.md"
        if skill_md.is_file():
            try:
                mtimes[skill_dir.name] = skill_md.stat().st_mtime_ns
            except OSError:
                continue
    return mtimes


__all__ = ["write_skill_metadata_cache"]
