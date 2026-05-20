from pathlib import Path

from teatree.skill_loading import DEFAULT_SKILLS_DIR, SkillLoadingPolicy
from teatree.types import SkillMetadata

__all__ = [
    "DEFAULT_SKILLS_DIR",
    "active_overlay_companion_skills",
    "resolve_skill_bundle",
]


def active_overlay_companion_skills() -> list[str]:
    """Return the active overlay's ``companion_skills`` list, or ``[]``.

    Resolved via the normal teatree overlay-discovery chain
    (``T3_OVERLAY_NAME`` env var, then cwd-based discovery). When no overlay
    is reachable — pre-bootstrap, tests without a configured overlay, a
    misconfigured environment — returns ``[]`` so the caller behaves as
    if no companions were declared.
    """
    try:
        from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return []
    try:
        overlay = get_overlay()
    except Exception:  # noqa: BLE001
        return []
    skills = getattr(overlay.config, "companion_skills", [])
    if not isinstance(skills, list):
        return []
    return [s for s in skills if isinstance(s, str) and s]


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
        companion_skills=active_overlay_companion_skills(),
    )
    return result.skills
