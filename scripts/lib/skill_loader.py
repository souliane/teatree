"""Skill suggestion engine for the UserPromptSubmit hook.

Called by ensure-skills-loaded.sh to detect user intent, resolve companion
and supplementary skills, and return a deduped suggestion list.

Runs with PYTHONPATH=$T3_REPO/scripts — no teetree package imports.
"""

from __future__ import annotations  # noqa: TID251 — standalone script, no teetree package imports

import json
import re
from pathlib import Path

XDG_DATA_DIR = Path.home() / ".local" / "share" / "teatree"
SKILL_METADATA_CACHE = XDG_DATA_DIR / "skill-metadata.json"


# ── Intent detection ─────────────────────────────────────────────────


def _detect_url_intent(prompt: str) -> str:
    """Detect intent from URLs in the prompt."""
    lp = prompt.lower()

    # GitLab issue/MR/job URLs (/-/issues/123, /-/merge_requests/456)
    if re.search(r"https?://gitlab\.[^\s]+/-/(issues|merge_requests|jobs)/\d+", lp):
        return "t3-ticket"
    # GitHub issue/PR URLs
    if re.search(r"https?://github\.com/[^\s]+/(issues|pull)/\d+", lp):
        return "t3-ticket"
    # Notion
    if re.search(r"https?://(www\.)?notion\.(so|site)/", lp):
        return "t3-ticket"
    # Confluence
    if re.search(r"https?://[^\s]*\.atlassian\.net/wiki/", lp):
        return "t3-ticket"
    # Linear
    if re.search(r"https?://linear\.app/[^\s]+/issue/", lp):
        return "t3-ticket"
    # Sentry
    if re.search(r"https?://[^\s]*sentry\.[^\s]+/issues/", lp):
        return "t3-debug"

    return ""


def _detect_keyword_intent(prompt: str) -> str:
    """Detect intent from prompt keywords. Order: specific → generic."""
    lp = prompt.lower()

    # t3-ship (but not "review this MR")
    if re.search(
        r"\b(merge request|pull request|create an? (mr|pr)|\bmr\b|push\b"
        r"|finalize|deliver|ship it|create mr|create pr)\b",
        lp,
    ) and not re.search(r"\breview\b", lp):
        return "t3-ship"
    if re.search(r"\bcommit\b", lp) and not re.search(r"\breview\b", lp):
        return "t3-ship"

    # t3-test
    if re.search(
        r"\b(run.*tests?|pytest|lint|sonar|e2e|ci fail|pipeline fail"
        r"|what tests|tests? broke|test runner)\b",
        lp,
    ):
        return "t3-test"
    if re.search(r"\bpipeline\b.*(fail|red|broke)", lp):
        return "t3-test"

    # t3-review-request (before t3-review — more specific)
    if re.search(
        r"\b(request review|ask for review|send.* review"
        r"|notify reviewer|post mr|review request)\b",
        lp,
    ):
        return "t3-review-request"

    # t3-review
    if re.search(
        r"\b(review|check the code|check my code|feedback"
        r"|quality check|code review)\b",
        lp,
    ):
        return "t3-review"

    # t3-debug
    if re.search(
        r"\b(broken|error|not working|crash|blank page|can.t connect"
        r"|debug|fix this|won.t start|500|traceback|exception)\b",
        lp,
    ):
        return "t3-debug"

    # t3-ticket (before t3-code — "implement TICKET-1234" is intake)
    if re.search(r"(new ticket|start working|what should i do)", lp):
        return "t3-ticket"
    if re.search(r"([a-z]+-\d+|\b(ticket|issue) #?\d+)", lp):
        return "t3-ticket"

    # t3-code
    if re.search(
        r"\b(implement|code it|feature|refactor|rework"
        r"|restructure|rewrite|redesign)\b",
        lp,
    ):
        return "t3-code"
    if re.search(
        r"\b(fix|change|update|modify|adjust|add|remove|delete|write|create"
        r"|build|move|rename|extract|split|merge|convert|migrate|optimize"
        r"|improve|replace|swap|introduce|drop|deprecate|wire|hook up"
        r"|integrate|extend|override|wrap|unwrap|inline|deduplicate|dedup"
        r"|simplify|generalize|normalize|transform|adapt|port|backport"
        r"|scaffold|stub|mock|patch|hotfix|tweak|rework|clean)"
        r" (the|a|an|this|that|my|our|its|some|all|each|every)\b",
        lp,
    ):
        return "t3-code"
    if re.match(
        r"^(fix|change|update|modify|adjust|add|remove|delete|write|create"
        r"|build|move|rename|extract|refactor|replace|introduce|extend"
        r"|override|simplify|optimize|improve|implement|convert|migrate"
        r"|integrate|wire|hook|patch|hotfix|tweak|rework|clean up"
        r"|scaffold|stub|mock|deduplicate|dedup) ",
        lp,
    ):
        return "t3-code"

    # t3-setup
    if re.search(
        r"\b(setup skills|configure claude|install skills"
        r"|bootstrap skills|configure hooks)\b",
        lp,
    ):
        return "t3-setup"

    # t3-contribute
    if re.search(
        r"\b(t3.?contribute|push improvements?|push skills?"
        r"|contribute upstream)\b",
        lp,
    ):
        return "t3-contribute"

    # t3-retro
    if re.search(
        r"\b(retro|retrospective|lessons learned|improve skills?"
        r"|auto.?improve|what went wrong)\b",
        lp,
    ):
        return "t3-retro"

    # t3-followup
    if re.search(
        r"\b(follow.?up|autopilot|batch tickets?|process all tickets"
        r"|not started issues?|work on all my tickets"
        r"|check (ticket )?status|advance tickets?"
        r"|remind reviewers?|mr reminders?|nudge)\b",
        lp,
    ):
        return "t3-followup"

    # t3-workspace
    if re.search(
        r"\b(worktree|setup|servers?|start session|refresh db|cleanup"
        r"|clean up|reset passwords?|restore.*(db|database))\b",
        lp,
    ):
        return "t3-workspace"
    if re.search(r"\b(database|start (the )?backend|start (the )?frontend)\b", lp):
        return "t3-workspace"

    return ""


def _detect_end_of_session(prompt: str) -> bool:
    """Detect standalone end-of-session phrases."""
    lp = prompt.strip().lower()
    return bool(
        re.match(
            r"(done|all set|finished|all done|wrap up|that.s it|that.s all"
            r"|ship it|we.re done|i.m done|looks good|lgtm)\s*[.!]?\s*$",
            lp,
        )
    )


def detect_intent(prompt: str) -> str:
    """Detect the primary skill intent from a user prompt."""
    url_intent = _detect_url_intent(prompt)
    if url_intent:
        return url_intent
    return _detect_keyword_intent(prompt)


# ── Companion skills (XDG cache) ────────────────────────────────────


def read_companion_skills() -> list[str]:
    """Read companion skills from the XDG skill-metadata cache."""
    if not SKILL_METADATA_CACHE.is_file():
        return []
    try:
        metadata = json.loads(SKILL_METADATA_CACHE.read_text(encoding="utf-8"))
        companions = metadata.get("companion_skills", [])
        return companions if isinstance(companions, list) else []
    except (json.JSONDecodeError, OSError):
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


# ── Dependency resolution ────────────────────────────────────────────


def _parse_skill_requires(skill_md_text: str) -> list[str]:
    """Extract the requires: list from SKILL.md YAML frontmatter."""
    if not skill_md_text.startswith("---"):
        return []
    try:
        end = skill_md_text.index("---", 3)
    except ValueError:
        return []
    frontmatter = skill_md_text[3:end]
    in_requires = False
    requires: list[str] = []
    for line in frontmatter.splitlines():
        stripped = line.strip()
        if stripped == "requires:":
            in_requires = True
            continue
        if in_requires:
            if stripped.startswith("- "):
                requires.append(stripped.removeprefix("- ").strip())
            else:
                break
    return requires


def _find_skill_md(name: str, search_dirs: list[Path]) -> Path | None:
    """Find SKILL.md for a skill name across search directories."""
    for d in search_dirs:
        candidate = d / name / "SKILL.md"
        if candidate.is_file():
            return candidate
    return None


def resolve_dependencies(skills: list[str], search_dirs: list[Path]) -> list[str]:
    """Recursively resolve requires: from SKILL.md frontmatter.

    Returns dependencies in topological order (deps before dependents).
    """
    resolved: list[str] = []
    seen: set[str] = set()

    def _walk(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        skill_md = _find_skill_md(name, search_dirs)
        if skill_md is not None:
            try:
                text = skill_md.read_text(encoding="utf-8")
            except OSError:
                text = ""
            for dep in _parse_skill_requires(text):
                _walk(dep)
        if name not in resolved:
            resolved.append(name)

    for skill in skills:
        _walk(skill)
    return resolved


# ── Overlay discovery (lightweight) ──────────────────────────────────


def _find_overlay_skill_dir(search_dirs: list[Path]) -> tuple[str, str]:
    """Find the overlay skill directory and project name from skill metadata cache.

    Returns (overlay_skill_dir, project_overlay) or ("", "").
    """
    if not SKILL_METADATA_CACHE.is_file():
        return "", ""
    try:
        metadata = json.loads(SKILL_METADATA_CACHE.read_text(encoding="utf-8"))
        skill_path = metadata.get("skill_path", "")
        if not skill_path:
            return "", ""
        # skill_path is relative to the host project (e.g., "overlay/SKILL.md")
        # We need to find the actual directory — check search_dirs
        for d in search_dirs:
            candidate = d / Path(skill_path).parent
            if candidate.is_dir():
                project_name = candidate.parent.name
                return str(candidate), project_name
        return "", ""
    except (json.JSONDecodeError, OSError):
        return "", ""


# ── Project-type detection ───────────────────────────────────────────


def _detect_framework_skills(cwd: str) -> list[str]:
    """Detect framework skills from project indicators in cwd or ancestors."""
    skills: list[str] = []
    search = Path(cwd)
    for directory in [search, *search.parents]:
        # Django project (has manage.py) or Django library (django in deps)
        if (directory / "manage.py").is_file():
            skills.append("ac-django")
            break
        pyproject = directory / "pyproject.toml"
        if pyproject.is_file():
            try:
                content = pyproject.read_text(encoding="utf-8")
                if re.search(r'["\']django[>=<]', content, re.IGNORECASE):
                    skills.append("ac-django")
            except OSError:
                pass
            break  # Stop at pyproject.toml regardless
        if directory == directory.parent:
            break
    return skills


# ── Main entry point ─────────────────────────────────────────────────


def suggest_skills(data: dict) -> dict:
    """Suggest skills based on user prompt and project context.

    Args:
        data: Hook input with keys: prompt, cwd, active_repos,
            loaded_skills, skill_search_dirs, supplementary_config.

    Returns:
        Dict with keys: suggestions, intent, overlay_skill_dir, project_overlay.

    """
    prompt = data.get("prompt", "")
    cwd = data.get("cwd", "")
    loaded = set(data.get("loaded_skills", []))
    search_dirs = [Path(d) for d in data.get("skill_search_dirs", [])]
    supplementary_config = data.get("supplementary_config", "")

    # 1. Detect intent
    intent = detect_intent(prompt)

    # End-of-session → t3-retro (only if other skills were loaded)
    if not intent and _detect_end_of_session(prompt):
        if loaded and "t3-retro" not in loaded:
            has_lifecycle = any(s.startswith("t3-") and s != "t3-retro" for s in loaded)
            if has_lifecycle:
                intent = "t3-retro"

    if not intent:
        return {"suggestions": [], "intent": "", "overlay_skill_dir": "", "project_overlay": ""}

    # 2. Build skill list: intent + companions + supplementary
    skills: list[str] = []

    # Always include t3-workspace as foundation (except t3-setup and t3-retro)
    if intent not in ("t3-setup", "t3-retro"):
        skills.append("t3-workspace")

    if intent != "t3-workspace":
        skills.append(intent)

    # Companion skills from overlay (XDG cache)
    skills.extend(read_companion_skills())

    # Supplementary skills from config
    skills.extend(read_supplementary_skills(supplementary_config, prompt))

    # Framework skills from project-type detection
    if cwd:
        skills.extend(_detect_framework_skills(cwd))

    # 3. Resolve dependencies (topological sort)
    resolved = resolve_dependencies(skills, search_dirs)

    # 4. Filter already-loaded and dedupe
    suggestions = []
    for skill in resolved:
        if skill not in loaded and skill not in suggestions:
            suggestions.append(skill)

    # 5. Overlay info for reference injections
    overlay_skill_dir, project_overlay = _find_overlay_skill_dir(search_dirs)

    return {
        "suggestions": suggestions,
        "intent": intent,
        "overlay_skill_dir": overlay_skill_dir,
        "project_overlay": project_overlay,
    }
