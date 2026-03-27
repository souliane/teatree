from pathlib import Path

from teetree.core.overlay import SkillMetadata
from teetree.skill_loading import (
    DEFAULT_SKILL_SEARCH_DIRS,
    DEFAULT_SKILLS_DIR,
    SkillLoadingPolicy,
    find_skill_md,
    parse_skill_requires,
    resolve_dependencies,
)

DEFAULT_DELEGATION_MAP = Path("references/skill-delegation.md")
__all__ = [
    "DEFAULT_DELEGATION_MAP",
    "DEFAULT_SKILLS_DIR",
    "find_skill_md",
    "parse_skill_requires",
    "resolve_dependencies",
    "resolve_skill_bundle",
]


def resolve_skill_bundle(
    *,
    phase: str,
    overlay_skill_metadata: SkillMetadata,
    delegation_map_path: Path | None = DEFAULT_DELEGATION_MAP,
    skills_dir: Path | list[Path] = DEFAULT_SKILL_SEARCH_DIRS,
) -> list[str]:
    del delegation_map_path
    policy = SkillLoadingPolicy(skills_dir=skills_dir)
    result = policy.select_for_runtime_phase(
        cwd=Path.cwd(),
        phase=phase,
        overlay_skill_metadata=overlay_skill_metadata,
    )
    return result.skills
