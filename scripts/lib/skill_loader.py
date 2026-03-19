"""Skill suggestion engine — intent detection, overlay discovery, dependency resolution.

Pure-Python replacement for the heavy-lifting portions of ensure-skills-loaded.sh.
No external dependencies (stdlib only) so it can run before any venv is activated.
"""

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Intent detection from prompt keywords (lines 206-304 of the bash script)
# ---------------------------------------------------------------------------

# Each entry: (skill_name, pattern, negative_pattern_or_None)
# Order matters — more specific patterns are checked first.
_INTENT_PATTERNS: list[tuple[str, re.Pattern[str], re.Pattern[str] | None]] = [
    # t3-ship: delivery actions (but not "review this MR" or "post mr" — those are review-request)
    (
        "t3-ship",
        re.compile(
            r"\b(merge request|pull request|create an? (mr|pr)"
            r"|\bmr\b|push\b|finalize|deliver|ship it|create mr|create pr)\b"
        ),
        re.compile(r"\breview|\bpost mr\b|\bpush (improvements?|skills?)\b|\bmr reminder"),
    ),
    # t3-ship: commit (but not "review comment")
    (
        "t3-ship",
        re.compile(r"\bcommit\b"),
        re.compile(r"\breview"),
    ),
    # t3-test: testing and CI
    (
        "t3-test",
        re.compile(
            r"\b(run.*tests?|pytest|lint|sonar|e2e|ci fail|pipeline fail|what tests|tests? broke|test runner)\b"
        ),
        None,
    ),
    # t3-test: "pipeline failed/failure/is red" (either order)
    (
        "t3-test",
        re.compile(r"\bpipeline\b.*(fail|red|broke)|(fail|red|broke).*\bpipeline\b"),
        None,
    ),
    # t3-test: "CI failed/failure" (allow word suffixes)
    (
        "t3-test",
        re.compile(r"\bci\b.*(fail|broke|red)"),
        None,
    ),
    # t3-review-request: request human review (more specific than t3-review)
    (
        "t3-review-request",
        re.compile(r"\b(request review|ask for review|send.* review|notify reviewer|post mr|review request)\b"),
        None,
    ),
    # t3-review: code review
    (
        "t3-review",
        re.compile(r"\b(review|check the code|check my code|feedback|quality check|code review)\b"),
        None,
    ),
    # t3-debug: troubleshooting
    (
        "t3-debug",
        re.compile(
            r"\b(broken|error|not working|crash|blank page|can.t connect"
            r"|debug|fix this|won.t start|500|traceback|exception)\b"
        ),
        None,
    ),
    # t3-ticket: ticket intake (check before t3-code)
    (
        "t3-ticket",
        re.compile(r"(new ticket|start working|what should i do)"),
        None,
    ),
    # t3-ticket: generic ticket/issue patterns (PROJ-1234, ticket #123, issue 456)
    (
        "t3-ticket",
        re.compile(r"([a-z]+-[0-9]+|\b(ticket|issue) #?[0-9]+)"),
        None,
    ),
    # t3-code: implementation keywords
    (
        "t3-code",
        re.compile(r"\b(implement|code it|feature|refactor|rework|restructure|rewrite|redesign)\b"),
        None,
    ),
    # t3-code: verb + article/pronoun patterns (exclude workspace/retro/setup triggers)
    (
        "t3-code",
        re.compile(
            r"\b(fix|change|update|modify|adjust|add|remove|delete|write|create|build|move|rename|extract|split|merge"
            r"|convert|migrate|optimize|improve|replace|swap|introduce|drop|deprecate|wire|hook up|integrate|extend"
            r"|override|wrap|unwrap|inline|deduplicate|dedup|simplify|generalize|normalize|transform|adapt|port"
            r"|backport|scaffold|stub|mock|patch|hotfix|tweak|rework|clean)"
            r" (the|a|an|this|that|my|our|its|some|all|each|every)\b"
        ),
        re.compile(r"\b(worktree|retro|retrospective)"),
    ),
    # t3-code: bare imperative verbs at start of prompt (exclude workspace/retro triggers)
    (
        "t3-code",
        re.compile(
            r"^(fix|change|update|modify|adjust|add|remove|delete|write|create|build|move|rename|extract|refactor"
            r"|replace|introduce|extend|override|simplify|optimize|improve|implement|convert|migrate|integrate|wire"
            r"|hook|patch|hotfix|tweak|rework|clean up|scaffold|stub|mock|deduplicate|dedup) "
        ),
        re.compile(r"\b(worktree|retro|retrospective)"),
    ),
    # t3-setup: first-time installation (check BEFORE t3-workspace)
    (
        "t3-setup",
        re.compile(r"\b(setup skills|configure claude|install skills|bootstrap skills|configure hooks)\b"),
        None,
    ),
    # t3-contribute: push improvements to fork / upstream issues
    (
        "t3-contribute",
        re.compile(r"\b(t3.?contribute|push improvements?|push skills?|contribute upstream)\b"),
        None,
    ),
    # t3-retro: retrospective and skill improvement
    (
        "t3-retro",
        re.compile(r"\b(retro|retrospective|lessons learned|improve skills?|auto.?improve|what went wrong)\b"),
        None,
    ),
    # t3-followup: daily follow-up, batch tickets, status checks
    (
        "t3-followup",
        re.compile(
            r"\b(follow.?up|autopilot|batch tickets?|process all tickets|not started issues?"
            r"|work on all my tickets|check (ticket )?status|advance tickets?"
            r"|remind reviewers?|mr reminders?|nudge)\b"
        ),
        None,
    ),
    # t3-workspace: environment/infrastructure
    (
        "t3-workspace",
        re.compile(
            r"\b(worktree|setup|servers?|start session|refresh db|cleanup|clean up|reset passwords?"
            r"|t3_setup|t3_ticket|wt_setup|ws_ticket|restore.*(db|database))\b"
        ),
        None,
    ),
    (
        "t3-workspace",
        re.compile(r"\b(database|start (the )?backend|start (the )?frontend)\b"),
        None,
    ),
]


def detect_intent(prompt: str) -> str:
    """Detect lifecycle skill intent from prompt keywords.

    Returns skill name (e.g. "t3-code") or empty string if no match.
    """
    lp = prompt.lower()
    for skill, pattern, negative in _INTENT_PATTERNS:
        if pattern.search(lp) and (negative is None or not negative.search(lp)):
            return skill
    return ""


# ---------------------------------------------------------------------------
# URL-based intent detection (lines 148-201)
# ---------------------------------------------------------------------------

_URL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # GitLab issue/MR/job URLs
    ("t3-ticket", re.compile(r"https?://gitlab\.\S+/-/(issues|merge_requests|jobs)/[0-9]+")),
    # GitHub issue/PR URLs
    ("t3-ticket", re.compile(r"https?://github\.com/[^\s]+/(issues|pull)/[0-9]+")),
    # Notion
    ("t3-ticket", re.compile(r"https?://(www\.)?notion\.(so|site)/")),
    # Confluence
    ("t3-ticket", re.compile(r"https?://[^\s]*\.atlassian\.net/wiki/")),
    # Linear
    ("t3-ticket", re.compile(r"https?://linear\.app/[^\s]+/issue/")),
    # Sentry
    ("t3-debug", re.compile(r"https?://[^\s]*sentry\.[^\s]+/issues/")),
]


def _check_overlay_url_patterns(lp: str, overlay_skill_dir: str) -> str:
    """Check overlay-provided URL patterns from hook-config/url-patterns.yml."""
    url_patterns_file = Path(overlay_skill_dir) / "hook-config" / "url-patterns.yml"
    if not url_patterns_file.is_file():
        return ""
    current_intent = ""
    for line in url_patterns_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r"^([a-z0-9-]+):", line)
        if m:
            current_intent = m.group(1)
            continue
        if current_intent:  # pragma: no branch
            m = re.match(r"^\s+-\s+(.*)", line)
            if m and re.search(m.group(1).strip("\"'"), lp):
                return current_intent
    return ""


def detect_url_intent(prompt: str, overlay_skill_dir: str = "") -> str:
    """Detect intent from URLs in the prompt.

    Checks built-in URL patterns first, then overlay-provided patterns
    from hook-config/url-patterns.yml.

    Returns skill name or empty string.
    """
    lp = prompt.lower()

    for skill, pattern in _URL_PATTERNS:
        if pattern.search(lp):
            return skill

    # Overlay-provided URL patterns
    if overlay_skill_dir:
        result = _check_overlay_url_patterns(lp, overlay_skill_dir)
        if result:
            return result

    return ""


# ---------------------------------------------------------------------------
# Overlay detection from context-match.yml (lines 66-102)
# ---------------------------------------------------------------------------


def _parse_cwd_patterns(match_file: Path) -> list[str]:
    """Parse cwd_patterns list from a context-match.yml file."""
    patterns: list[str] = []
    in_patterns = False
    for line in match_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line.startswith("cwd_patterns:"):
            in_patterns = True
            continue
        # Any other top-level key ends the patterns section
        if re.match(r"^[a-z]", line):
            in_patterns = False
            continue
        if in_patterns:
            m = re.match(r"^\s+-\s+(.*)", line)
            if m:  # pragma: no branch
                pat = m.group(1).strip("\"'")
                patterns.append(pat)
    return patterns


def detect_overlay(cwd: str, active_repos: list[str], skill_search_dirs: list[str] | None = None) -> str:
    """Detect project overlay by matching cwd/active_repos against context-match.yml patterns.

    *skill_search_dirs* lists directories that contain skill subdirectories
    (e.g. ``["~/teatree", "~/.agents/skills"]``).

    Returns the overlay skill name, or empty string if no match.
    """
    if skill_search_dirs is None:
        skill_search_dirs = []

    for skills_root in skill_search_dirs:
        root = Path(skills_root)
        if not root.is_dir():
            continue
        for candidate_dir in sorted(root.iterdir()):
            if not candidate_dir.is_dir():
                continue
            match_file = candidate_dir / "hook-config" / "context-match.yml"
            if not match_file.is_file():
                continue
            for pat in _parse_cwd_patterns(match_file):
                if pat in cwd:
                    return candidate_dir.name
                if any(pat in repo for repo in active_repos):
                    return candidate_dir.name
    return ""


# ---------------------------------------------------------------------------
# Skill dependency resolution (lines 378-413)
# ---------------------------------------------------------------------------


def get_skill_deps(skill_name: str, skill_search_dirs: list[str] | None = None) -> list[str]:  # noqa: C901
    """Parse ``requires:`` from SKILL.md YAML frontmatter.

    Returns list of dependency skill names (one level deep, no transitive resolution).
    """
    if skill_search_dirs is None:
        skill_search_dirs = []

    skill_md: Path | None = None
    for root in skill_search_dirs:
        candidate = Path(root) / skill_name / "SKILL.md"
        if candidate.is_file():
            skill_md = candidate
            break

    if skill_md is None:
        return []

    deps: list[str] = []
    in_frontmatter = False
    in_requires = False
    for line in skill_md.read_text(encoding="utf-8").splitlines():  # pragma: no branch
        if line == "---":
            if in_frontmatter:
                break
            in_frontmatter = True
            continue
        if not in_frontmatter:
            continue
        if line.startswith("requires:"):
            in_requires = True
            continue
        if re.match(r"^[a-z]", line):
            in_requires = False
            continue
        if in_requires:
            m = re.match(r"^\s+-\s+(.*)", line)
            if m:  # pragma: no branch
                dep = m.group(1).strip("\"'")
                deps.append(dep)
    return deps


# ---------------------------------------------------------------------------
# Companion skill resolution (lines 451-492)
# ---------------------------------------------------------------------------


def resolve_companion_skills(
    overlay_skill_dir: str,
    cwd: str,
    active_repos: list[str],
) -> list[str]:
    """Parse companion_skills from context-match.yml and return matching skill names."""
    match_file = Path(overlay_skill_dir) / "hook-config" / "context-match.yml"
    if not match_file.is_file():
        return []

    companions: list[str] = []
    in_companion = False
    current_skill = ""

    for line in match_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Top-level key
        if re.match(r"^[a-z]", line):
            in_companion = bool(line.startswith("companion_skills:"))
            current_skill = ""
            continue
        if not in_companion:
            continue
        # Skill name key (2-space indent): "  ac-django:"
        m = re.match(r"^\s{2}([a-z][a-z0-9_-]+):", line)
        if m:
            current_skill = m.group(1)
            continue
        # Pattern list item (4-space indent): "    - my-backend"
        if current_skill:  # pragma: no branch
            m = re.match(r"^\s+-\s+(.*)", line)
            if m:  # pragma: no branch
                pat = m.group(1).strip("\"'")
                matched = pat in cwd or any(pat in repo for repo in active_repos)
                if matched:
                    companions.append(current_skill)
                    current_skill = ""  # don't add same skill twice
    return companions


# ---------------------------------------------------------------------------
# Supplementary skills from user config (lines 308-340)
# ---------------------------------------------------------------------------


def detect_supplementary_skills(prompt: str, config_path: str) -> list[str]:
    r"""Detect keyword-triggered supplementary skills from user config file.

    Config format (simple YAML):
        my-ruff-skill: '\\b(ruff|lint(er)? adopt)\\b'
        my-pdf-skill: '\\b(acroform|pdf template)\\b'
    """
    path = Path(config_path)
    if not path.is_file():
        return []

    lp = prompt.lower()
    skills: list[str] = []

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r"^([a-zA-Z][a-zA-Z0-9_-]+):\s+(.*)", line)
        if m:  # pragma: no branch
            skill_name = m.group(1)
            pattern = m.group(2).strip("\"'")
            if re.search(pattern, lp):
                skills.append(skill_name)
    return skills


# ---------------------------------------------------------------------------
# Main orchestrator (lines 416-508)
# ---------------------------------------------------------------------------


def build_suggestion(  # noqa: PLR0913, PLR0917, C901
    intent: str,
    project_context: bool,
    project_overlay: str,
    overlay_skill_dir: str,
    loaded_skills: list[str],
    supplementary_skills: list[str],
    skill_search_dirs: list[str] | None = None,
) -> list[str]:
    """Assemble the list of skills to suggest.

    Returns an ordered list of skill names that should be loaded.
    """
    if skill_search_dirs is None:
        skill_search_dirs = []

    loaded = set(loaded_skills)
    suggest: list[str] = []

    def _add(name: str) -> None:
        if name not in loaded and name not in suggest:
            suggest.append(name)

    # No intent and no project context → nothing to suggest
    if not intent and not project_context:
        return suggest

    # Always suggest t3-workspace as foundation (except for standalone skills)
    if intent not in {"", "t3-setup", "t3-retro"} and "t3-workspace" not in loaded:
        _add("t3-workspace")

    # Suggest the detected intent skill
    if intent and intent != "t3-workspace":
        _add(intent)

    # Resolve dependencies (one level deep)
    if intent:  # pragma: no branch
        for dep in get_skill_deps(intent, skill_search_dirs=skill_search_dirs):
            _add(dep)

    # In project context, suggest the overlay
    if project_context and project_overlay:
        _add(project_overlay)

    # In project context, suggest companion skills
    if project_context and overlay_skill_dir:
        for comp in resolve_companion_skills(overlay_skill_dir, "", []):
            _add(comp)

    # Append supplementary skills
    for supp in supplementary_skills:
        _add(supp)

    return suggest


# ---------------------------------------------------------------------------
# JSON entry point for bash integration
# ---------------------------------------------------------------------------


def suggest_skills(data: dict[str, object]) -> dict[str, object]:
    """Entry point called from bash via ``python3 -c "from lib.skill_loader import suggest_skills"``.

    Accepts a dict with keys:
        prompt, cwd, active_repos, overlay_skill_dir, loaded_skills,
        project_context, project_overlay, skill_search_dirs,
        supplementary_config

    Returns dict with: intent, url_intent, suggestions
    """
    prompt = str(data.get("prompt", ""))
    overlay_skill_dir = str(data.get("overlay_skill_dir", ""))
    loaded_skills: list[str] = list(data.get("loaded_skills", []))  # type: ignore[arg-type]
    project_context = bool(data.get("project_context"))
    project_overlay = str(data.get("project_overlay", ""))
    skill_search_dirs: list[str] = list(data.get("skill_search_dirs", []))  # type: ignore[arg-type]
    supp_config = str(data.get("supplementary_config", ""))

    # URL intent first, then keyword intent
    url_intent = detect_url_intent(prompt, overlay_skill_dir=overlay_skill_dir)
    intent = url_intent or detect_intent(prompt)

    # Default to t3-code in project context with no intent
    if project_context and not intent:
        intent = "t3-code"

    supplementary = detect_supplementary_skills(prompt, supp_config) if supp_config else []

    suggestions = build_suggestion(
        intent=intent,
        project_context=project_context,
        project_overlay=project_overlay,
        overlay_skill_dir=overlay_skill_dir,
        loaded_skills=loaded_skills,
        supplementary_skills=supplementary,
        skill_search_dirs=skill_search_dirs,
    )

    return {
        "intent": intent,
        "suggestions": suggestions,
    }
