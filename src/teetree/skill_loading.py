"""Centralised skill selection policy for all TeaTree entry points.

Three callers route through ``SkillLoadingPolicy``:

* ``t3 agent`` CLI (interactive launch)
* ``scripts/lib/skill_loader.py`` (UserPromptSubmit hook)
* ``agents/skill_bundle.py`` (headless runtime)
"""

import re
import subprocess  # noqa: S404
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from teetree.core.overlay import SkillMetadata

DEFAULT_SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"
DEFAULT_SKILL_SEARCH_DIRS = [DEFAULT_SKILLS_DIR, Path.home() / ".agents" / "skills", Path.home() / ".claude" / "skills"]

AGENT_LAUNCH = "agent_launch"
PROMPT_HOOK = "prompt_hook"
RUNTIME_PHASE = "runtime_phase"

_AGENT_TASK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "t3-debug": ("debug", "fix", "error", "broken", "crash", "not working", "bug", "trace"),
    "t3-test": ("test", "pytest", "e2e", "lint", "ci", "pipeline", "qa"),
    "t3-ship": ("commit", "push", "ship", "deliver", "mr", "merge request", "pull request"),
    "t3-review": ("review", "feedback", "check the code"),
    "t3-ticket": ("ticket", "issue", "start working on"),
    "t3-retro": ("retro", "retrospective", "lessons learned"),
    "t3-workspace": ("setup", "worktree", "create worktree", "servers", "cleanup"),
}

_STATUS_TO_SKILL: dict[str, str] = {
    "not_started": "t3-ticket",
    "scoped": "t3-ticket",
    "started": "t3-code",
    "coded": "t3-test",
    "tested": "t3-review",
    "reviewed": "t3-ship",
    "shipped": "t3-debug",
    "in_review": "t3-debug",
    "merged": "t3-debug",
    "delivered": "t3-debug",
}

_PHASE_TO_SKILL: dict[str, str] = {
    "ticket-intake": "t3-ticket",
    "scoping": "t3-ticket",
    "coding": "t3-code",
    "testing": "t3-test",
    "reviewing": "t3-review",
    "shipping": "t3-ship",
    "debugging": "t3-debug",
    "requesting_review": "t3-review-request",
    "retrospecting": "t3-retro",
}

_PYTHON_FILE_HINTS = ("pyproject.toml", "setup.py", "requirements.txt")
_DJANGO_DEPENDENCY_RE = re.compile(r'["\']django[>=<]', re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SkillSelectionResult:
    skills: list[str]
    lifecycle_skill: str = ""
    ask_user: bool = False


type OverlaySkillMetadata = SkillMetadata | dict[str, object]


def parse_skill_requires(skill_md_text: str) -> list[str]:
    """Extract the ``requires:`` list from SKILL.md YAML frontmatter."""
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


def find_skill_md(name_or_path: str, skills_dir: Path | list[Path]) -> Path | None:
    """Locate SKILL.md for a skill name or a direct file path."""
    as_path = Path(name_or_path)
    if as_path.is_file():
        return as_path
    if as_path.name == "SKILL.md" and as_path.parent.is_dir():
        return as_path if as_path.exists() else None

    search_dirs = skills_dir if isinstance(skills_dir, list) else [skills_dir]
    for directory in search_dirs:
        candidate = directory / name_or_path / "SKILL.md"
        if candidate.is_file():
            return candidate
    return None


def resolve_dependencies(
    skills: list[str],
    *,
    skills_dir: Path | list[Path] = DEFAULT_SKILL_SEARCH_DIRS,
) -> list[str]:
    """Recursively resolve ``requires:`` from SKILL.md frontmatter."""
    resolved: list[str] = []
    seen: set[str] = set()

    def _walk(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        skill_md = find_skill_md(name, skills_dir)
        if skill_md is not None:
            for dep in parse_skill_requires(skill_md.read_text(encoding="utf-8")):
                _walk(dep)
        resolved.append(name)

    for skill in skills:
        _walk(skill)
    return resolved


class SkillLoadingPolicy:
    """Single source of truth for skill selection decisions."""

    def __init__(self, *, skills_dir: Path | list[Path] = DEFAULT_SKILL_SEARCH_DIRS) -> None:
        self.skills_dir = skills_dir

    def select_for_agent_launch(  # noqa: PLR0913
        self,
        *,
        cwd: Path,
        overlay_skill_metadata: OverlaySkillMetadata,
        task: str,
        ticket_status: str,
        explicit_phase: str,
        explicit_skills: list[str],
        overlay_active: bool,
    ) -> SkillSelectionResult:
        if explicit_phase and explicit_skills:
            msg = "--phase and --skill cannot be used together"
            raise ValueError(msg)

        lifecycle_skill = ""
        ask_user = False
        if explicit_phase:
            lifecycle_skill = self.lifecycle_for_phase(explicit_phase)
            if not lifecycle_skill:
                msg = f"Unknown phase: {explicit_phase}"
                raise ValueError(msg)
        elif explicit_skills:
            lifecycle_skill = ""
        elif ticket_status:
            lifecycle_skill = self.lifecycle_for_status(ticket_status)
        elif task:
            lifecycle_skill = self.lifecycle_for_task_text(task)
        else:
            ask_user = True

        if not lifecycle_skill and not explicit_skills and not ask_user:
            ask_user = True

        ordered = self._base_detected_skills(
            cwd=cwd,
            overlay_skill_metadata=overlay_skill_metadata,
            overlay_active=overlay_active,
            lifecycle_skill=lifecycle_skill,
        )
        if explicit_skills:
            ordered.extend(explicit_skills)
        elif lifecycle_skill:
            ordered.append(lifecycle_skill)

        return SkillSelectionResult(
            skills=self._resolve_and_dedupe(ordered),
            lifecycle_skill=lifecycle_skill,
            ask_user=ask_user,
        )

    def select_for_prompt_hook(
        self,
        *,
        cwd: Path,
        intent: str,
        overlay_skill_metadata: OverlaySkillMetadata,
        loaded_skills: set[str],
        supplementary_skills: list[str] | None = None,
    ) -> SkillSelectionResult:
        ordered = self._base_detected_skills(
            cwd=cwd,
            overlay_skill_metadata=overlay_skill_metadata,
            overlay_active=False,
            lifecycle_skill=intent,
        )
        if intent:
            ordered.append(intent)
        if supplementary_skills:
            ordered.extend(supplementary_skills)
        resolved = self._resolve_and_dedupe(ordered)
        suggestions = [skill for skill in resolved if skill not in loaded_skills]
        return SkillSelectionResult(skills=suggestions, lifecycle_skill=intent)

    def select_for_runtime_phase(
        self,
        *,
        cwd: Path,
        phase: str,
        overlay_skill_metadata: OverlaySkillMetadata,
    ) -> SkillSelectionResult:
        lifecycle_skill = self.lifecycle_for_phase(phase)
        ordered = self._base_detected_skills(
            cwd=cwd,
            overlay_skill_metadata=overlay_skill_metadata,
            overlay_active=True,
            lifecycle_skill=lifecycle_skill,
        )
        if lifecycle_skill:
            ordered.append(lifecycle_skill)
        return SkillSelectionResult(
            skills=self._resolve_and_dedupe(ordered),
            lifecycle_skill=lifecycle_skill,
        )

    @staticmethod
    def lifecycle_for_status(status: str) -> str:
        return _STATUS_TO_SKILL.get(status, "")

    @staticmethod
    def lifecycle_for_phase(phase: str) -> str:
        return _PHASE_TO_SKILL.get(phase, "")

    @staticmethod
    def lifecycle_for_task_text(task: str) -> str:
        lowered = task.lower()
        for skill_name, keywords in _AGENT_TASK_KEYWORDS.items():
            if any(keyword in lowered for keyword in keywords):
                return skill_name
        return ""

    def _base_detected_skills(
        self,
        *,
        cwd: Path,
        overlay_skill_metadata: OverlaySkillMetadata,
        overlay_active: bool,
        lifecycle_skill: str,
    ) -> list[str]:
        ordered: list[str] = []
        overlay_skill = self._overlay_skill_for_context(
            cwd=cwd,
            overlay_skill_metadata=overlay_skill_metadata,
            overlay_active=overlay_active,
            lifecycle_skill=lifecycle_skill,
        )
        if overlay_skill:
            ordered.append(overlay_skill)
        ordered.extend(self.detect_framework_skills(cwd))
        return ordered

    def _overlay_skill_for_context(
        self,
        *,
        cwd: Path,
        overlay_skill_metadata: OverlaySkillMetadata,
        overlay_active: bool,
        lifecycle_skill: str,
    ) -> str:
        skill_path = str(overlay_skill_metadata.get("skill_path", "")).strip()
        if not skill_path:
            return ""
        if overlay_active:
            return skill_path
        if not lifecycle_skill:
            return ""
        patterns_object = overlay_skill_metadata.get("remote_patterns", [])
        if not isinstance(patterns_object, list):
            return ""
        patterns = [pattern for pattern in patterns_object if isinstance(pattern, str) and pattern]
        if not patterns:
            return ""
        return skill_path if self._matches_any_remote(cwd, patterns) else ""

    @staticmethod
    def detect_framework_skills(cwd: Path) -> list[str]:
        for directory in [cwd, *cwd.parents]:
            if (directory / "manage.py").is_file():
                return ["ac-django"]
            pyproject = directory / "pyproject.toml"
            if pyproject.is_file():
                try:
                    content = pyproject.read_text(encoding="utf-8")
                except OSError:
                    return []
                if _DJANGO_DEPENDENCY_RE.search(content):
                    return ["ac-django"]
                return ["ac-python"]
            if any((directory / candidate).is_file() for candidate in _PYTHON_FILE_HINTS[1:]):
                return ["ac-python"]
        return []

    def _resolve_and_dedupe(self, skills: list[str]) -> list[str]:
        ordered: list[str] = []
        for skill in resolve_dependencies(skills, skills_dir=self.skills_dir):
            # ac-adopting-ruff is a one-shot migration skill (ruff adoption),
            # not a session companion — skip it during automatic resolution.
            if skill == "ac-adopting-ruff":
                continue
            if skill not in ordered:
                ordered.append(skill)
        return ordered

    @staticmethod
    def _matches_any_remote(cwd: Path, patterns: list[str]) -> bool:
        urls = SkillLoadingPolicy._git_remote_urls(cwd)
        return any(any(fnmatch(url, pattern) for pattern in patterns) for url in urls)

    @staticmethod
    def _git_remote_urls(cwd: Path) -> list[str]:
        origin_url = SkillLoadingPolicy._git_remote_url(cwd, "origin")
        if origin_url:
            return [origin_url]
        command = ["git", "-C", str(cwd), "remote", "-v"]
        try:
            proc = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
        except OSError:
            return []
        if proc.returncode != 0:
            return []
        seen: set[str] = set()
        urls: list[str] = []
        for raw_line in proc.stdout.splitlines():
            parts = raw_line.split()
            if len(parts) >= 2 and parts[1] not in seen:  # noqa: PLR2004
                seen.add(parts[1])
                urls.append(parts[1])
        return urls

    @staticmethod
    def _git_remote_url(cwd: Path, remote_name: str) -> str:
        command = ["git", "-C", str(cwd), "remote", "get-url", remote_name]
        try:
            proc = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
        except OSError:
            return ""
        if proc.returncode != 0:
            return ""
        return proc.stdout.strip()
