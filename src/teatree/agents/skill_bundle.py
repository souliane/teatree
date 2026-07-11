from pathlib import Path

from teatree.skill_support.deps import SkillIndex
from teatree.skill_support.loading import DEFAULT_SKILLS_DIR, SkillLoadingPolicy
from teatree.types import SkillMetadata

__all__ = [
    "DEFAULT_SKILLS_DIR",
    "active_overlay_companion_skills",
    "active_overlay_lifecycle_skills",
    "active_overlay_pr_review_companion",
    "active_overlay_review_skills",
    "resolve_skill_bundle",
]


def _active_overlay_config() -> object | None:
    """Return the active overlay's ``config`` instance, or ``None``.

    Hermetic accessor shared by the companion-skill resolvers below: the
    overlay-loader import and ``get_overlay()`` call can each fail in
    pre-bootstrap / test environments without a configured overlay, and the
    caller's contract is to behave as if no overlay declared anything.
    """
    try:
        from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — overlay loader may be unavailable pre-bootstrap; degrade to no overlay
        return None
    try:
        overlay = get_overlay()
    except Exception:  # noqa: BLE001 — no configured overlay degrades to no config
        return None
    return getattr(overlay, "config", None)


def active_overlay_companion_skills() -> list[str]:
    """Return the active overlay's ``companion_skills`` list, or ``[]``.

    Resolved via the normal teatree overlay-discovery chain
    (``T3_OVERLAY_NAME`` env var, then cwd-based discovery). When no overlay
    is reachable — pre-bootstrap, tests without a configured overlay, a
    misconfigured environment — returns ``[]`` so the caller behaves as
    if no companion skills were declared.
    """
    config = _active_overlay_config()
    if config is None:
        return []
    skills = getattr(config, "companion_skills", [])
    if not isinstance(skills, list):
        return []
    return [s for s in skills if isinstance(s, str) and s]


def active_overlay_pr_review_companion() -> str:
    """Return the active overlay's ``pr_review_companion``, or ``""``.

    The reviewer-dispatch companion (#1135). When no overlay is reachable
    the caller behaves as if no companion was declared — the reviewer
    sub-agent still loads ``/t3:review`` but no review-quality skill is
    injected. The class-level default (``"code-review"``) only applies when
    an overlay is reachable.
    """
    config = _active_overlay_config()
    if config is None:
        return ""
    value = getattr(config, "pr_review_companion", "")
    return value if isinstance(value, str) else ""


def active_overlay_review_skills() -> list[str]:
    """Return the active overlay's ``get_review_companion_skills()``, or ``[]``.

    The deduped ordered ``[pr_review_companion, *companion_skills]`` a headless
    reviewer must hold. Mirrors :func:`active_overlay_pr_review_companion` but
    returns the full review-skill set so the reviewing-phase bundle AND the
    reviewing-phase system context embed every overlay review skill in full
    rather than demoting them to a one-line summary. When no overlay is
    reachable the caller behaves as if no review companion skills were declared.
    """
    config = _active_overlay_config()
    if config is None:
        return []
    getter = getattr(config, "get_review_companion_skills", None)
    if not callable(getter):
        return []
    try:
        skills = getter()
    except Exception:  # noqa: BLE001 — an unreadable skill list degrades to none
        return []
    if not isinstance(skills, list):
        return []
    return [s for s in skills if isinstance(s, str) and s]


def active_overlay_lifecycle_skills(lifecycle: str) -> list[str]:
    """Return the active overlay's ``get_lifecycle_companion_skills(lifecycle)``.

    Generalizes :func:`active_overlay_review_skills` to every lifecycle so a
    fanned-out ``code``/``e2e``/``test`` task — not only a reviewing task —
    demands the overlay's companion skills. When no overlay is reachable, or the
    overlay predates the hook, the caller behaves as if none were declared.
    """
    config = _active_overlay_config()
    if config is None:
        return []
    getter = getattr(config, "get_lifecycle_companion_skills", None)
    if not callable(getter):
        return []
    try:
        skills = getter(lifecycle)
    except Exception:  # noqa: BLE001 — an unreadable skill list degrades to none
        return []
    if not isinstance(skills, list):
        return []
    return [s for s in skills if isinstance(s, str) and s]


def _dispatch_cwd(worktree_path: str | Path | None) -> Path:
    """The detection root for framework + overlay skill discovery (PR-12).

    A dispatched task runs in its OWN worktree, so skills must be detected from
    the ticket's checkout, never the orchestrator's ambient cwd (the loop's
    clone). Falls back to the ambient cwd when no worktree exists yet
    (pre-provision) or the recorded path is gone — the byte-identical
    pre-PR-12 behaviour.
    """
    if worktree_path:
        candidate = Path(worktree_path)
        if candidate.is_dir():
            return candidate
    return Path.cwd()


def resolve_skill_bundle(
    *,
    phase: str,
    overlay_skill_metadata: SkillMetadata,
    skill_index: SkillIndex | None = None,
    worktree_path: str | Path | None = None,
) -> list[str]:
    policy = SkillLoadingPolicy()
    result = policy.select_for_runtime_phase(
        cwd=_dispatch_cwd(worktree_path),
        phase=phase,
        overlay_skill_metadata=overlay_skill_metadata,
        skill_index=skill_index,
        companion_skills=active_overlay_companion_skills(),
        pr_review_companion=active_overlay_pr_review_companion(),
        review_skills=active_overlay_review_skills(),
    )
    return result.skills
