"""Skill metadata, loading policy, dependency resolution, and schema validation.

Package facade re-exporting the cross-package public surface so callers import
from ``teatree.skill_support`` while each symbol keeps an explicit defining
submodule (``deps`` / ``loading`` / ``map`` / ``ref_validator`` / ``schema``,
prefix stripped from the former flat ``skill_*`` modules). ``mock.patch``
targets and ``python -m`` entry points name the defining submodule, never this
facade.
"""

from teatree.skill_support.deps import companion_suggestions, resolve_all, resolve_requires
from teatree.skill_support.loading import DEFAULT_SKILLS_DIR, SkillLoadingPolicy, SkillSelectionResult
from teatree.skill_support.map import load_skill_delegation, parse_skill_delegation_map, render_skill_delegation_map
from teatree.skill_support.ref_validator import validate_skill_refs
from teatree.skill_support.schema import validate_directory, validate_skill_md

__all__ = [
    "DEFAULT_SKILLS_DIR",
    "SkillLoadingPolicy",
    "SkillSelectionResult",
    "companion_suggestions",
    "load_skill_delegation",
    "parse_skill_delegation_map",
    "render_skill_delegation_map",
    "resolve_all",
    "resolve_requires",
    "validate_directory",
    "validate_skill_md",
    "validate_skill_refs",
]
