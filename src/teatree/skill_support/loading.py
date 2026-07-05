"""Centralised skill selection policy for all TeaTree entry points.

Two callers route through ``SkillLoadingPolicy``:

* ``t3 agent`` CLI (interactive launch)
* ``scripts/lib/skill_loader.py`` (UserPromptSubmit hook)

Skill selection is fully explicit — slash commands, phase mapping, ticket
status, the requires-dependency chain, and cwd/overlay context. There is no
free-text keyword scan of the task/prompt text; a launch with neither a phase,
a skill, nor a ticket status asks the user which lifecycle to run.
"""

import logging
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from teatree.skill_support.deps import SkillIndex, companion_suggestions, resolve_requires
from teatree.types import SkillMetadata
from teatree.utils import git

logger = logging.getLogger(__name__)


def _default_skills_dir() -> Path:
    from teatree import find_project_root  # noqa: PLC0415

    root = find_project_root()
    if root:
        return root / "skills"
    # Fallback for non-source installs: skills/ next to src/
    return Path(__file__).resolve().parents[3] / "skills"


DEFAULT_SKILLS_DIR = _default_skills_dir()

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
    "planning": "architecture-design",
    "coding": "code",
    "testing": "test",
    "e2e": "e2e",
    "reviewing": "review",
    "shipping": "ship",
    "debugging": "debug",
    "requesting_review": "review-request",
    "retrospecting": "retro",
}

_PYTHON_FILE_HINTS = ("pyproject.toml", "setup.py", "requirements.txt")
_DJANGO_DEPENDENCY_RE = re.compile(r'["\']django[>=<]', re.IGNORECASE)
_FASTAPI_DEPENDENCY_RE = re.compile(r'(?:^|["\'])fastapi[>=<~\[]', re.IGNORECASE | re.MULTILINE)

# Every skill name ``detect_framework_skills`` can emit. The dispatch-prompt
# builder classifies a resolved bundle against this set to force the stack's
# coding skill to load explicitly rather than be demoted to an ignorable
# summary (#1368).
FRAMEWORK_SKILL_NAMES = frozenset({"ac-django", "ac-python", "fastapi"})


def _framework_skills_for_content(content: str) -> list[str]:
    if _DJANGO_DEPENDENCY_RE.search(content):
        return ["ac-django"]
    if _FASTAPI_DEPENDENCY_RE.search(content):
        return ["ac-python", "fastapi"]
    return ["ac-python"]


def _framework_skills_for_directory(directory: Path) -> list[str] | None:
    if (directory / "manage.py").is_file():
        return ["ac-django"]
    for candidate in _PYTHON_FILE_HINTS:
        path = directory / candidate
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return [] if candidate == "pyproject.toml" else ["ac-python"]
        return _framework_skills_for_content(content)
    return None


@dataclass(frozen=True, slots=True)
class SkillSelectionResult:
    skills: list[str]
    lifecycle_skill: str = ""
    ask_user: bool = False
    advisory_skills: tuple[str, ...] = ()
    #: SOFT companion suggestions of the resolved skills — surfaced, never loaded
    #: as a hard dependency (that is what ``requires`` → ``skills`` is for).
    companion_suggestions: tuple[str, ...] = ()


type OverlaySkillMetadata = SkillMetadata | dict[str, object]


class SkillLoadingPolicy:
    """Single source of truth for skill selection decisions."""

    @staticmethod
    def _resolve_requires_chain(
        skills: list[str],
        skill_index: SkillIndex,
    ) -> list[str]:
        """Resolve the transitive ``requires`` chain, warning on deps with no SKILL.md.

        A required skill absent from *skill_index* has no SKILL.md in this repo
        (an external methodology skill like ``test-driven-development``, or a
        framework skill). It passes through so the ``Skill`` tool still loads
        it; the warning surfaces the missing definition without dropping it.
        """
        resolved = resolve_requires(skills, skill_index)
        known = {str(e.get("skill", "")) for e in skill_index if e.get("skill")}
        for skill in resolved:
            if skill not in known and skill not in FRAMEWORK_SKILL_NAMES:
                logger.warning("Required skill %r has no SKILL.md — continuing", skill)
        return resolved

    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def select_for_agent_launch(  # noqa: PLR0913
        self,
        *,
        cwd: Path,
        overlay_skill_metadata: OverlaySkillMetadata,
        ticket_status: str,
        explicit_phase: str,
        explicit_skills: list[str],
        overlay_active: bool,
        skill_index: SkillIndex | None = None,
        companion_skills: list[str] | None = None,
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
        else:
            ask_user = True

        if not lifecycle_skill and not explicit_skills and not ask_user:
            ask_user = True

        ordered = self._base_detected_skills(
            cwd=cwd,
            overlay_skill_metadata=overlay_skill_metadata,
            overlay_active=overlay_active,
            lifecycle_skill=lifecycle_skill,
            companion_skills=companion_skills,
        )
        if explicit_skills:
            ordered.extend(explicit_skills)
        elif lifecycle_skill:
            ordered.append(lifecycle_skill)

        resolved = self._resolve_requires_chain(ordered, skill_index or [])
        return SkillSelectionResult(
            skills=_dedupe(resolved),
            lifecycle_skill=lifecycle_skill,
            ask_user=ask_user,
        )

    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def select_for_prompt_hook(  # noqa: PLR0913
        self,
        *,
        cwd: Path,
        overlay_skill_metadata: OverlaySkillMetadata,
        loaded_skills: set[str],
        supplementary_skills: list[str] | None = None,
        skill_index: SkillIndex | None = None,
        companion_skills: list[str] | None = None,
    ) -> SkillSelectionResult:
        """Framework + overlay + cwd context skills for a prompt, no prose scan.

        Surfaces the cwd/overlay-detected skills (``ac-django`` for a Django
        cwd, the overlay's own skill + companion skills for an overlay repo)
        plus any advisory supplementary skills — never a free-text keyword
        match on the prompt.
        """
        hard = self._base_detected_skills(
            cwd=cwd,
            overlay_skill_metadata=overlay_skill_metadata,
            overlay_active=False,
            lifecycle_skill="",
            companion_skills=companion_skills,
        )
        hard_resolved = set(self._resolve_requires_chain(hard, skill_index or []))

        ordered = [*hard, *(supplementary_skills or [])]
        resolved = _dedupe(self._resolve_requires_chain(ordered, skill_index or []))
        suggestions = [skill for skill in resolved if skill not in loaded_skills]
        advisory = tuple(skill for skill in suggestions if skill not in hard_resolved)
        companions = tuple(
            skill for skill in companion_suggestions(resolved, skill_index or []) if skill not in loaded_skills
        )
        return SkillSelectionResult(
            skills=suggestions,
            advisory_skills=advisory,
            companion_suggestions=companions,
        )

    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def select_for_runtime_phase(  # noqa: PLR0913
        self,
        *,
        cwd: Path,
        phase: str,
        overlay_skill_metadata: OverlaySkillMetadata,
        skill_index: SkillIndex | None = None,
        companion_skills: list[str] | None = None,
        pr_review_companion: str = "",
        review_skills: list[str] | None = None,
    ) -> SkillSelectionResult:
        lifecycle_skill = self.lifecycle_for_phase(phase)
        ordered = self._base_detected_skills(
            cwd=cwd,
            overlay_skill_metadata=overlay_skill_metadata,
            overlay_active=True,
            lifecycle_skill=lifecycle_skill,
            companion_skills=companion_skills,
        )
        if lifecycle_skill:
            ordered.append(lifecycle_skill)
        # #1135: a reviewer sub-agent dispatch (phase resolving to the
        # ``review`` lifecycle skill) also loads the project's review skills.
        # Sub-agents do not auto-load skills, so the caller (``run_headless``
        # via ``resolve_skill_bundle``) inlines those SKILL.md bodies into the
        # dispatched prompt via ``_read_skill_contents_scoped``. ``review_skills``
        # (the overlay's full deduped review set) supersedes the single
        # ``pr_review_companion`` when supplied.
        if lifecycle_skill == "review":
            if review_skills:
                ordered.extend(review_skills)
            elif pr_review_companion:
                ordered.append(pr_review_companion)
        resolved = self._resolve_requires_chain(ordered, skill_index or [])
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

    def _base_detected_skills(
        self,
        *,
        cwd: Path,
        overlay_skill_metadata: OverlaySkillMetadata,
        overlay_active: bool,
        lifecycle_skill: str,
        companion_skills: list[str] | None = None,
    ) -> list[str]:
        ordered: list[str] = []
        overlay_in_scope = self._overlay_in_scope(
            cwd=cwd,
            overlay_skill_metadata=overlay_skill_metadata,
            overlay_active=overlay_active,
            lifecycle_skill=lifecycle_skill,
        )
        if overlay_in_scope:
            skill_path = str(overlay_skill_metadata.get("skill_path", "")).strip()
            if skill_path:
                ordered.append(skill_path)
        ordered.extend(self.detect_framework_skills(cwd))
        if overlay_in_scope and companion_skills:
            ordered.extend(s for s in companion_skills if isinstance(s, str) and s)
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
        if not SkillLoadingPolicy._overlay_in_scope(
            cwd=cwd,
            overlay_skill_metadata=overlay_skill_metadata,
            overlay_active=overlay_active,
            lifecycle_skill=lifecycle_skill,
        ):
            return ""
        return skill_path

    @staticmethod
    def _overlay_in_scope(
        *,
        cwd: Path,
        overlay_skill_metadata: OverlaySkillMetadata,
        overlay_active: bool,
        lifecycle_skill: str,
    ) -> bool:
        """Whether an overlay repo is actually in scope for this task.

        The overlay companion skills (and the overlay's own skill) are
        required ONLY for overlay work — when the resolved overlay is active
        for the session, or the cwd's git remote matches one of the overlay's
        ``remote_patterns``. Teatree-core-only work (no overlay-active, no
        matching remote) is NOT overlay work, so its companion-skill load
        gate must not fire.
        """
        if overlay_active:
            return True
        if not lifecycle_skill:
            return False
        patterns_object = overlay_skill_metadata.get("remote_patterns", [])
        if not isinstance(patterns_object, list):
            return False
        patterns = [pattern for pattern in patterns_object if isinstance(pattern, str) and pattern]
        if not patterns:
            return False
        return _matches_any_remote(cwd, patterns)

    @staticmethod
    def detect_framework_skills(cwd: Path) -> list[str]:
        for directory in [cwd, *cwd.parents]:
            skills = _framework_skills_for_directory(directory)
            if skills is not None:
                return skills
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
    origin_url = git.remote_url(repo=str(cwd), remote="origin")
    if origin_url:
        return [origin_url]
    raw = git.run(repo=str(cwd), args=["remote", "-v"])
    if not raw:
        return []
    seen: set[str] = set()
    urls: list[str] = []
    for raw_line in raw.splitlines():
        parts = raw_line.split()
        if len(parts) >= 2 and parts[1] not in seen:  # noqa: PLR2004
            seen.add(parts[1])
            urls.append(parts[1])
    return urls
