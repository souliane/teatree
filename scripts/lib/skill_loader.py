"""Skill suggestion engine for the UserPromptSubmit hook.

Called by ensure-skills-loaded.sh to detect user intent and return a
deduped suggestion list via ``SkillLoadingPolicy``.

Trigger patterns are read from SKILL.md frontmatter (``triggers:`` field),
not hardcoded.  A cached trigger index in the XDG data directory is used
when available; otherwise skills are scanned on the fly from
``skill_search_dirs``.
"""

from __future__ import annotations  # noqa: TID251

import json
import re
import sys
from pathlib import Path

from lib.trigger_parser import parse_triggers as parse_triggers_from_frontmatter

_SRC_DIR = Path(__file__).resolve().parents[2] / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from teatree.skill_loading import DEFAULT_SKILLS_DIR, SkillLoadingPolicy

XDG_DATA_DIR = Path.home() / ".local" / "share" / "teatree"
SKILL_METADATA_CACHE = XDG_DATA_DIR / "skill-metadata.json"


def _get_installed_version() -> str:
    """Return the installed teatree package version, or ``""`` on failure."""
    try:
        import importlib.metadata

        return importlib.metadata.version("teatree")
    except Exception:  # noqa: BLE001
        return ""


def _read_metadata_cache() -> dict:
    """Read and validate the XDG skill-metadata cache.

    Returns an empty dict when the cache is missing, corrupt, or was
    written by a different teatree version.
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
    return metadata


# End-of-session phrases (matched when no keyword/URL intent fires and a
# skill declares ``end_of_session: true``).
_LIFECYCLE_SKILLS = frozenset(
    {
        "ticket",
        "code",
        "test",
        "debug",
        "review",
        "ship",
        "review-request",
        "retro",
        "workspace",
        "next",
        "contribute",
        "followup",
        "handover",
        "setup",
    }
)

_END_OF_SESSION_RE = re.compile(
    r"^(done|all set|finished|all done|wrap up|that.s it|that.s all"
    r"|ship it|we.re done|i.m done|looks good|lgtm)\s*[.!]?\s*$",
)


def build_trigger_index(skill_search_dirs: list[Path]) -> list[dict]:
    """Scan skill directories and build a trigger index from SKILL.md frontmatter.

    Returns a list of dicts sorted by priority, each with keys:
    ``skill``, ``priority``, ``keywords``, ``urls``, ``exclude``, ``end_of_session``.
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
            triggers = parse_triggers_from_frontmatter(text)
            if triggers is None:
                continue
            seen.add(skill_name)
            index.append({"skill": skill_name, **triggers})

    import operator

    index.sort(key=operator.itemgetter("priority"))
    return index


def _read_trigger_index() -> list[dict]:
    """Read the cached trigger index from the XDG data directory."""
    metadata = _read_metadata_cache()
    index = metadata.get("trigger_index", [])
    return index if isinstance(index, list) else []


# ── Intent detection (data-driven) ──────────────────────────────────


def detect_intent(
    prompt: str,
    *,
    trigger_index: list[dict] | None = None,
    skill_search_dirs: list[Path] | None = None,
    loaded_skills: set[str] | None = None,
) -> str:
    """Detect the primary skill intent from a user prompt.

    Uses the trigger index (from cache or built on the fly) to match
    URL and keyword patterns.  Returns the matching skill name or ``""``.
    """
    if trigger_index is None:
        trigger_index = _read_trigger_index()
        if not trigger_index and skill_search_dirs:
            trigger_index = build_trigger_index(skill_search_dirs)

    if not trigger_index:
        return ""

    lp = prompt.lower()

    # Pass 0: Explicit /<skill> slash commands (highest priority).
    # When the prompt starts with a known skill name, use it directly
    # instead of falling through to URL/keyword matching.
    slash_match = re.match(r"^/?([a-z][a-z0-9_-]+)", lp.strip())
    if slash_match:
        candidate = slash_match.group(1)
        indexed = {e["skill"] for e in trigger_index}
        if candidate in indexed:
            return candidate

    # Pass 1: URL patterns (checked first, across all skills by priority)
    for entry in trigger_index:
        for url_pattern in entry.get("urls", []):
            try:
                if re.search(url_pattern, lp):
                    return entry["skill"]
            except re.error:
                continue

    # Pass 2: Keyword patterns (by priority, with exclude support)
    for entry in trigger_index:
        exclude = entry.get("exclude", "")
        if exclude:
            try:
                if re.search(exclude, lp):
                    continue
            except re.error:
                pass

        for kw_pattern in entry.get("keywords", []):
            try:
                if re.search(kw_pattern, lp):
                    return entry["skill"]
            except re.error:
                continue

    # Pass 3: End-of-session detection for skills with end_of_session: true
    if _END_OF_SESSION_RE.match(prompt.strip().lower()):
        loaded = loaded_skills or set()
        has_lifecycle = any(s in _LIFECYCLE_SKILLS for s in loaded)
        if has_lifecycle:
            for entry in trigger_index:
                if entry.get("end_of_session") and entry["skill"] not in loaded:
                    return entry["skill"]

    return ""


# ── Companion skills (XDG cache) ────────────────────────────────────


def read_companion_skills() -> list[str]:
    """Read companion skills from the XDG skill-metadata cache."""
    metadata = _read_metadata_cache()
    companions = metadata.get("companion_skills", [])
    return companions if isinstance(companions, list) else []


def read_overlay_skill_metadata() -> dict[str, object]:
    """Read overlay skill metadata from the XDG cache."""
    metadata = _read_metadata_cache()
    return {
        "skill_path": metadata.get("skill_path", ""),
        "remote_patterns": metadata.get("remote_patterns", []),
    }


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
    """Suggest skills based on user prompt and project context.

    Args:
        data: Hook input with keys: prompt, cwd, active_repos,
            loaded_skills, skill_search_dirs, supplementary_config.

    Returns:
        Dict with keys: suggestions, intent.

    """
    prompt = data.get("prompt", "")
    cwd = data.get("cwd", "")
    loaded = set(data.get("loaded_skills", []))
    search_dirs = [Path(d) for d in data.get("skill_search_dirs", [])]
    supplementary_config = data.get("supplementary_config", "")

    # 1. Detect intent from trigger index
    intent = detect_intent(
        prompt,
        skill_search_dirs=search_dirs,
        loaded_skills=loaded,
    )

    if not intent:
        return {"suggestions": [], "intent": ""}

    combined_search_dirs: list[Path] = []
    for directory in [*search_dirs, DEFAULT_SKILLS_DIR]:
        if directory not in combined_search_dirs:
            combined_search_dirs.append(directory)
    policy = SkillLoadingPolicy()
    selection = policy.select_for_prompt_hook(
        cwd=Path(cwd) if cwd else Path.cwd(),
        intent=intent,
        overlay_skill_metadata=read_overlay_skill_metadata(),
        loaded_skills=loaded,
        supplementary_skills=read_supplementary_skills(supplementary_config, prompt),
    )
    return {"suggestions": selection.skills, "intent": intent}
