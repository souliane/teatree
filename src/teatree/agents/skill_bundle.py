from pathlib import Path

from teatree.core.overlay import SkillMetadata
from teatree.skill_loading import DEFAULT_SKILLS_DIR, SkillLoadingPolicy

__all__ = [
    "DEFAULT_SKILLS_DIR",
    "resolve_skill_bundle",
]


def resolve_skill_bundle(
    *,
    phase: str,
    overlay_skill_metadata: SkillMetadata,
    trigger_index: list[dict[str, object]] | None = None,
) -> list[str]:
    policy = SkillLoadingPolicy()
    result = policy.select_for_runtime_phase(
        cwd=Path.cwd(),
        phase=phase,
        overlay_skill_metadata=overlay_skill_metadata,
        trigger_index=trigger_index,
    )
    return result.skills
