"""Skill suggestion engine for the SessionStart hook.

Called by ``hooks/scripts/session_start_skills.py`` to surface the skills a
session's cwd/overlay context implies — framework skills (``ac-django`` /
``ac-python``), the active overlay's own skill, and its companion skills.
There is no free-text scan of any prompt:
lifecycle skills load explicitly via slash commands, ``t3 agent --phase/--skill``,
and the transitive ``requires`` chain.

The skill (requires) index is read from a cached index in the XDG data
directory when it is fresh; otherwise the policy builds the same index live
over every skill root.
"""

from __future__ import annotations  # noqa: TID251 — standalone script, not a teatree package module

import json
import os
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[2] / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from teatree.skill_support.index import harness_skills_dirs, skill_mtimes
from teatree.skill_support.loading import SkillLoadingPolicy


def xdg_data_dir() -> Path:
    """The teatree data dir, resolved the way the Django writer resolves it.

    ``teatree.paths.resolve_data_dir`` honours ``XDG_DATA_HOME`` before
    falling back to ``~/.local/share``; this reader must too, or an install
    with an XDG sandbox has ``t3 config write-skill-cache`` writing one file
    while the SessionStart hook reads another — and a cache the reader
    cannot find is indistinguishable from a cache with no skills in it
    (souliane/teatree#3829). Resolved per call rather than at import so the
    answer tracks the environment the hook actually runs under.
    """
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "teatree"


def skill_metadata_cache() -> Path:
    """The skill-metadata cache file ``teatree.core.skill_cache`` writes."""
    return xdg_data_dir() / "skill-metadata.json"


def _get_installed_version() -> str:
    """Return the installed teatree package version, or ``""`` on failure."""
    try:
        import importlib.metadata

        return importlib.metadata.version("teatree")
    except Exception:  # noqa: BLE001 — best-effort helper: a failure is swallowed so the caller degrades, never aborts
        return ""


def _read_metadata_cache() -> dict:
    """Read and validate the XDG skill-metadata cache.

    Returns an empty dict when the cache is missing, corrupt, was
    written by a different teatree version, or has stale mtimes.
    """
    cache_path = skill_metadata_cache()
    if not cache_path.is_file():
        return {}
    try:
        metadata = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(metadata, dict):
        return {}
    cached_version = metadata.get("teatree_version", "")
    if cached_version and cached_version != _get_installed_version():
        return {}
    if _cache_is_stale(metadata):
        return {}
    return metadata


def _cache_is_stale(metadata: dict) -> bool:
    """Whether any SKILL.md in any skill root was added, removed or edited since the cache was written."""
    cached_mtimes = metadata.get("skill_mtimes")
    if not isinstance(cached_mtimes, dict):
        return False  # No mtimes stored — can't check, assume fresh.
    return skill_mtimes(harness_skills_dirs()) != cached_mtimes


def _read_skill_index() -> list[dict] | None:
    """The cached skill (requires) index, or ``None`` so the policy builds it live."""
    index = _read_metadata_cache().get("skill_index")
    return index if isinstance(index, list) else None


def read_overlay_skill_metadata() -> dict[str, object]:
    """Read overlay skill metadata from the XDG cache."""
    metadata = _read_metadata_cache()
    return {
        "skill_path": metadata.get("skill_path", ""),
        "remote_patterns": metadata.get("remote_patterns", []),
    }


def read_overlay_companion_skills() -> list[str]:
    """Return the active overlay's ``companion_skills`` list, or ``[]``.

    Delegates to :func:`teatree.agents.skill_bundle.active_overlay_companion_skills`,
    which resolves the active overlay (via ``T3_OVERLAY_NAME`` then cwd-based
    discovery) and reads the ``companion_skills`` field from its config. Safe
    to call pre-bootstrap or when no overlay is configured — returns ``[]``.
    """
    try:
        from teatree.agents.skill_bundle import active_overlay_companion_skills
    except Exception:  # noqa: BLE001 — best-effort helper: a failure is swallowed so the caller degrades, never aborts
        return []
    try:
        return active_overlay_companion_skills()
    except Exception:  # noqa: BLE001 — best-effort helper: a failure is swallowed so the caller degrades, never aborts
        return []


# ── Main entry point ─────────────────────────────────────────────────


def suggest_skills(data: dict) -> dict:
    """Suggest the skills a session's cwd/overlay context implies.

    Args:
        data: Hook input with keys: cwd, loaded_skills, skill_search_dirs.

    Returns:
        Dict with keys: suggestions, companions. ``companions`` are the SOFT
        companion suggestions of the resolved skills — surfaced, never a hard
        demand (unlike ``requires`` → ``suggestions``).

    """
    cwd = data.get("cwd", "")
    loaded = set(data.get("loaded_skills", []))
    tool_input = data.get("tool_input", {}) or {}
    file_path = str(tool_input.get("file_path", "") or "")

    selection = SkillLoadingPolicy().select_for_session_start(
        cwd=_detect_cwd(file_path, cwd),
        overlay_skill_metadata=read_overlay_skill_metadata(),
        loaded_skills=loaded,
        skill_index=_read_skill_index(),
        companion_skills=read_overlay_companion_skills(),
    )
    return {"suggestions": selection.skills, "companions": list(selection.companion_suggestions)}


def _detect_cwd(file_path: str, fallback_cwd: str) -> Path:
    """Return the directory to run framework detection against.

    Prefer ``file_path``'s parent (Edit/Write on a specific file) so the
    detector walks from that location toward the repo root. Fall back to
    the hook's reported ``cwd`` and finally to ``Path.cwd()``.
    """
    if file_path:
        candidate = Path(file_path)
        if candidate.is_dir():
            return candidate
        return candidate.parent
    if fallback_cwd:
        return Path(fallback_cwd)
    return Path.cwd()
