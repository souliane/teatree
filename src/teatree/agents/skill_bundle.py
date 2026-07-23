import logging
from pathlib import Path

from teatree.agents.phase_agent_skills import declared_skills_for_phase
from teatree.skill_support.deps import SkillIndex
from teatree.skill_support.loading import DEFAULT_SKILLS_DIR, SkillLoadingPolicy
from teatree.types import SkillMetadata

__all__ = [
    "DEFAULT_SKILLS_DIR",
    "active_overlay_companion_skills",
    "active_overlay_pr_review_companion",
    "active_overlay_review_skills",
    "active_overlay_stage_skills",
    "resolve_skill_bundle",
]

logger = logging.getLogger(__name__)


def _active_overlay_config() -> object | None:
    """Return the active overlay's ``config`` instance, or ``None``.

    Hermetic accessor shared by the companion-skill resolvers below: the
    overlay-loader import and ``get_overlay()`` call can each fail in
    pre-bootstrap / test environments without a configured overlay, and the
    caller's contract is to behave as if no overlay declared anything.
    """
    try:
        from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred: call-time import, kept lazy
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


def active_overlay_stage_skills(phase: str) -> list[str]:
    """Return the active overlay's ADDITIONAL skills for *phase*, or ``[]``.

    Reads ``config.get_stage_skills(phase)`` on the ACTIVE overlay only — a
    teatree-core (public repo) dispatch runs under the teatree overlay, whose
    stage map never carries a team overlay's skills, so team-internal skill
    bodies cannot leak into a public-repo work prompt. When no overlay is
    reachable — pre-bootstrap, tests without a configured overlay — returns
    ``[]``. A configured stage skill that resolves to no ``SKILL.md`` in any
    search dir is an operator config error: it is logged (fail loud) but still
    returned, so the requires-chain / preamble path also surfaces it.
    """
    config = _active_overlay_config()
    if config is None:
        return []
    getter = getattr(config, "get_stage_skills", None)
    if not callable(getter):
        return []
    try:
        skills = getter(phase)
    except Exception:  # noqa: BLE001 — an unreadable stage map degrades to none
        return []
    if not isinstance(skills, list):
        return []
    resolved = [s for s in skills if isinstance(s, str) and s]
    _warn_unresolvable_stage_skills(resolved, phase)
    return resolved


def _warn_unresolvable_stage_skills(skills: list[str], phase: str) -> None:
    from teatree.agents.skill_injection import (  # noqa: PLC0415 — deferred: keeps module import light
        _resolve_skill_md,
        harness_skills_dirs,
    )

    dirs = harness_skills_dirs()
    for name in skills:
        if _resolve_skill_md(name, dirs) is None:
            logger.warning("Stage skill %r for phase %r resolves to no SKILL.md — continuing", name, phase)


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
    stage_skills: list[str] | None = None,
) -> list[str]:
    """Resolve the phase's skill bundle for a dispatch.

    *stage_skills* threads the dispatch's single ``active_overlay_stage_skills``
    resolution (#3206) so a dispatch that also builds the prompts does not
    re-resolve (re-warn / re-read SKILL.md) per builder; when absent it is
    resolved here.

    The phase's ``agents/*.md`` declaration is authoritative for the lifecycle
    skills (#3667) — the same declaration the interactive lane resolves, so a
    headless coding dispatch loads ``architecture-design`` rather than only
    ``code``.
    """
    if stage_skills is None:
        stage_skills = active_overlay_stage_skills(phase)
    policy = SkillLoadingPolicy()
    result = policy.select_for_runtime_phase(
        cwd=_dispatch_cwd(worktree_path),
        phase=phase,
        overlay_skill_metadata=overlay_skill_metadata,
        skill_index=skill_index,
        companion_skills=active_overlay_companion_skills(),
        pr_review_companion=active_overlay_pr_review_companion(),
        review_skills=active_overlay_review_skills(),
        stage_skills=stage_skills,
        agent_declared_skills=declared_skills_for_phase(phase),
    )
    return result.skills
