"""Centralised skill selection policy for all TeaTree entry points.

Two callers route through ``SkillLoadingPolicy``:

* ``t3 agent`` CLI (interactive launch)
* ``scripts/lib/skill_loader.py`` (UserPromptSubmit hook)
"""

import re
import subprocess  # noqa: S404
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from teatree.skill_deps import resolve_requires
from teatree.types import SkillMetadata

DEFAULT_SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"

_AGENT_TASK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "debug": ("debug", "fix", "error", "broken", "crash", "not working", "bug", "trace"),
    "test": ("test", "pytest", "e2e", "lint", "ci", "pipeline", "qa"),
    "ship": ("commit", "push", "ship", "deliver", "mr", "merge request", "pull request"),
    "review": ("review", "feedback", "check the code"),
    "ticket": ("ticket", "issue", "start working on"),
    "retro": ("retro", "retrospective", "lessons learned"),
    "workspace": ("setup", "worktree", "create worktree", "servers", "cleanup"),
}

_STATUS_TO_SKILL: dict[str, str] = {
    "not_started": "ticket",
    "scoped": "ticket",
    "started": "code",
    "coded": "test",
    "tested": "review",
    "reviewed": "ship",
    "shipped": "debug",
    "in_review": "debug",
    "merged": "debug",
    "delivered": "debug",
}

_PHASE_TO_SKILL: dict[str, str] = {
    "ticket-intake": "ticket",
    "scoping": "ticket",
    "coding": "code",
    "testing": "test",
    "reviewing": "review",
    "shipping": "ship",
    "debugging": "debug",
    "requesting_review": "review-request",
    "retrospecting": "retro",
}

_PYTHON_FILE_HINTS = ("pyproject.toml", "setup.py", "requirements.txt")
_DJANGO_DEPENDENCY_RE = re.compile(r'["\']django[>=<]', re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SkillSelectionResult:
    skills: list[str]
    lifecycle_skill: str = ""
    ask_user: bool = False


type OverlaySkillMetadata = SkillMetadata | dict[str, object]


class SkillLoadingPolicy:
    """Single source of truth for skill selection decisions."""

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
        trigger_index: list[dict[str, object]] | None = None,
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
            lifecycle_skill = self.lifecycle_for_task_text(task, trigger_index=trigger_index)
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

        resolved = resolve_requires(ordered, trigger_index or [])
        return SkillSelectionResult(
            skills=_dedupe(resolved),
            lifecycle_skill=lifecycle_skill,
            ask_user=ask_user,
        )

    def select_for_prompt_hook(  # noqa: PLR0913
        self,
        *,
        cwd: Path,
        intent: str,
        overlay_skill_metadata: OverlaySkillMetadata,
        loaded_skills: set[str],
        supplementary_skills: list[str] | None = None,
        trigger_index: list[dict[str, object]] | None = None,
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
        resolved = _dedupe(resolve_requires(ordered, trigger_index or []))
        suggestions = [skill for skill in resolved if skill not in loaded_skills]
        return SkillSelectionResult(skills=suggestions, lifecycle_skill=intent)

    def select_for_runtime_phase(
        self,
        *,
        cwd: Path,
        phase: str,
        overlay_skill_metadata: OverlaySkillMetadata,
        trigger_index: list[dict[str, object]] | None = None,
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
        resolved = resolve_requires(ordered, trigger_index or [])
        return SkillSelectionResult(
            skills=_dedupe(resolved),
            lifecycle_skill=lifecycle_skill,
        )

    @staticmethod
    def lifecycle_for_status(status: str) -> str:
        return _STATUS_TO_SKILL.get(status, "")

    @staticmethod
    def lifecycle_for_phase(phase: str) -> str:
        return _PHASE_TO_SKILL.get(phase, "")

    @staticmethod
    def lifecycle_for_task_text(
        task: str,
        *,
        trigger_index: list[dict[str, object]] | None = None,
    ) -> str:
        lowered = task.lower()
        # Pass 1: hardcoded keywords (fast, no I/O).
        for skill_name, keywords in _AGENT_TASK_KEYWORDS.items():
            if any(keyword in lowered for keyword in keywords):
                return skill_name
        # Pass 2: search_hints from skill frontmatter (trigger index).
        if trigger_index:
            for entry in trigger_index:
                hints = entry.get("search_hints", [])
                if not isinstance(hints, list):
                    continue
                skill = str(entry.get("skill", ""))
                if any(isinstance(h, str) and h.lower() in lowered for h in hints):
                    return skill
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

    @staticmethod
    def _overlay_skill_for_context(
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
        return skill_path if _matches_any_remote(cwd, patterns) else ""

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


def _dedupe(skills: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for skill in skills:
        if skill not in seen:
            seen.add(skill)
            result.append(skill)
    return result


def _matches_any_remote(cwd: Path, patterns: list[str]) -> bool:
    urls = _git_remote_urls(cwd)
    return any(any(fnmatch(url, pattern) for pattern in patterns) for url in urls)


def _git_remote_urls(cwd: Path) -> list[str]:
    origin_url = _git_remote_url(cwd, "origin")
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


def _git_remote_url(cwd: Path, remote_name: str) -> str:
    command = ["git", "-C", str(cwd), "remote", "get-url", remote_name]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
    except OSError:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()
