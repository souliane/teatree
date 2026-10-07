"""Skill metadata cache.

Writes the active overlay's skill metadata + skill (requires) index to
``$DATA_DIR/skill-metadata.json``. The SessionStart hook reads the
cache to resolve overlay matching and the requires closure without paying
the cost of Django bootstrap. The index is the one every dispatch uses —
built over every skill root by :func:`teatree.skill_support.index.build_skill_index`.

Written only by `t3 config write-skill-cache` (apm's ``post_install``); a stale
cache makes the reader build the index live.
"""

import json
import logging
from collections.abc import Sequence
from pathlib import Path

import teatree
from teatree.core.overlay_loader import get_overlay
from teatree.paths import DATA_DIR
from teatree.skill_support.deps import resolve_all
from teatree.skill_support.index import build_skill_index, harness_skills_dirs, resolve_skill_md, skill_mtimes
from teatree.skill_support.schema import validate_skill_md

logger = logging.getLogger(__name__)


def write_skill_metadata_cache() -> None:
    """Write the active overlay's skill metadata to the XDG data directory."""
    roots = harness_skills_dirs()
    metadata = get_overlay().metadata.get_skill_metadata()
    skill_index = build_skill_index(roots)
    _validate_skills(roots, {str(entry["skill"]) for entry in skill_index})
    metadata["skill_index"] = skill_index
    metadata["resolved_requires"] = resolve_all(skill_index)
    metadata["skill_mtimes"] = skill_mtimes(roots)
    metadata["teatree_version"] = teatree.__version__
    cache_path = DATA_DIR / "skill-metadata.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def _validate_skills(roots: Sequence[Path], known_skills: set[str]) -> None:
    for name in sorted(known_skills):
        skill_md = resolve_skill_md(name, roots)
        if skill_md is None:
            continue
        errors, warnings = validate_skill_md(skill_md, known_skills=known_skills)
        for warning in warnings:
            logger.warning("%s", warning)
        for error in errors:
            logger.warning("Skill validation error: %s", error)


__all__ = ["write_skill_metadata_cache"]
