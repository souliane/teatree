"""Skill suggestion engine for the UserPromptSubmit hook.

Called by hook_router.py (UserPromptSubmit event) to surface the skills a
prompt's cwd/overlay context implies — framework skills (``ac-django`` /
``ac-python``), the active overlay's own skill, and its companion skills —
plus advisory supplementary skills. There is no free-text scan of the prompt:
lifecycle skills load explicitly via slash commands, ``t3 agent --phase/--skill``,
and the transitive ``requires`` chain.

The skill (requires) index is read from a cached index in the XDG data
directory when available; otherwise skills are scanned on the fly from
``skill_search_dirs``.
"""

from __future__ import annotations  # noqa: TID251 — standalone script, not a teatree package module

import json
import operator
import re
import sys
from pathlib import Path

from lib.requires_parser import parse_companions, parse_requires

_SRC_DIR = Path(__file__).resolve().parents[2] / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from teatree.skill_support.loading import SkillLoadingPolicy

XDG_DATA_DIR = Path.home() / ".local" / "share" / "teatree"
SKILL_METADATA_CACHE = XDG_DATA_DIR / "skill-metadata.json"


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
    if not SKILL_METADATA_CACHE.is_file():
        return {}
    try:
        metadata = json.loads(SKILL_METADATA_CACHE.read_text(encoding="utf-8"))
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
    """Check if any SKILL.md file has been modified since the cache was written."""
    cached_mtimes = metadata.get("skill_mtimes", {})
    if not isinstance(cached_mtimes, dict):
        return False  # No mtimes stored — can't check, assume fresh.
    home = Path.home()
    skills_dir = home / ".claude" / "skills"
    if not skills_dir.is_dir():
        return False
    for skill_dir in skills_dir.iterdir():
        resolved = skill_dir.resolve() if skill_dir.is_symlink() else skill_dir
        if not resolved.is_dir():
            continue
        skill_md = resolved / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            current_mtime = skill_md.stat().st_mtime_ns
        except OSError:
            continue
        cached_mtime = cached_mtimes.get(skill_dir.name)
        if cached_mtime is None or current_mtime != cached_mtime:
            return True
    return False


def build_requires_index(skill_search_dirs: list[Path]) -> list[dict]:
    """Scan skill directories and index each skill's ``requires:`` list.

    Returns a list of ``{"skill": name, "requires": [...]}`` dicts, one per
    discovered SKILL.md, sorted by skill name.
    """
    seen: set[str] = set()
    index: list[dict] = []

    for search_dir in skill_search_dirs:
        if not search_dir.is_dir():
            continue
        for skill_dir in sorted(search_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_name = skill_dir.name
            if skill_name in seen:
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue
            try:
                text = skill_md.read_text(encoding="utf-8")
            except OSError:
                continue
            seen.add(skill_name)
            index.append(
                {
                    "skill": skill_name,
                    "requires": parse_requires(text) or [],
                    "companions": parse_companions(text) or [],
                }
            )

    index.sort(key=operator.itemgetter("skill"))
    return index


def _read_skill_index() -> list[dict]:
    """Read the cached skill (requires) index from the XDG data directory."""
    metadata = _read_metadata_cache()
    index = metadata.get("skill_index", [])
    return index if isinstance(index, list) else []


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


# ── Supplementary skills (config file) ──────────────────────────────


def read_supplementary_skills(config_path: str, prompt: str) -> list[str]:
    """Read keyword-triggered supplementary skills from config."""
    if not config_path or not Path(config_path).is_file():
        return []

    lp = prompt.lower()
    matched: list[str] = []

    try:
        for line in Path(config_path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^([a-zA-Z][a-zA-Z0-9_-]+):\s+(.*)", line)
            if not m:
                continue
            skill_name = m.group(1)
            pattern = m.group(2).strip("'\"")
            try:
                if re.search(pattern, lp):
                    matched.append(skill_name)
            except re.error:
                continue
    except OSError:
        pass

    return matched


# ── Main entry point ─────────────────────────────────────────────────


def suggest_skills(data: dict) -> dict:
    r"""Suggest the skills a prompt's cwd/overlay context implies.

    Args:
        data: Hook input with keys: prompt, cwd, active_repos,
            loaded_skills, skill_search_dirs, supplementary_config.

    Returns:
        Dict with keys: suggestions, advisory, companions. ``advisory`` is the
        subset of ``suggestions`` sourced ONLY from the supplementary keyword
        config (``$HOME/.teatree-skills.yml``). Those loose user-authored keyword
        regexes (e.g. ``\bruff\b``) over-fire on incidental mentions, so the
        caller suggests them but never adds them to the hard-block demand set.
        ``companions`` are the SOFT companion suggestions of the resolved skills
        — surfaced, never a hard demand (unlike ``requires`` → ``suggestions``).

    """
    prompt = data.get("prompt", "")
    cwd = data.get("cwd", "")
    loaded = set(data.get("loaded_skills", []))
    supplementary_config = data.get("supplementary_config", "")
    tool_input = data.get("tool_input", {}) or {}
    file_path = str(tool_input.get("file_path", "") or "")

    detect_cwd = _detect_cwd(file_path, cwd)
    policy = SkillLoadingPolicy()
    selection = policy.select_for_prompt_hook(
        cwd=detect_cwd,
        overlay_skill_metadata=read_overlay_skill_metadata(),
        loaded_skills=loaded,
        supplementary_skills=read_supplementary_skills(supplementary_config, prompt),
        skill_index=_read_skill_index(),
        companion_skills=read_overlay_companion_skills(),
    )
    return {
        "suggestions": selection.skills,
        "advisory": list(selection.advisory_skills),
        "companions": list(selection.companion_suggestions),
    }


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
