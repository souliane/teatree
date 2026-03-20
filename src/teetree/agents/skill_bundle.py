from pathlib import Path

from teetree.core.overlay import SkillMetadata
from teetree.skill_map import DEFAULT_SKILL_MAP, load_skill_delegation

DEFAULT_DELEGATION_MAP = DEFAULT_SKILL_MAP
DEFAULT_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "skills"


def load_delegation_map(path: Path | None = DEFAULT_SKILL_MAP) -> dict[str, list[str]]:
    _source_path, mapping = load_skill_delegation(path)
    return mapping


def parse_skill_requires(skill_md: str) -> list[str]:
    """Extract the ``requires:`` list from SKILL.md YAML frontmatter."""
    if not skill_md.startswith("---"):
        return []
    end = skill_md.index("---", 3)
    frontmatter = skill_md[3:end]
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


def find_skill_md(name_or_path: str, skills_dir: Path) -> Path | None:
    """Locate SKILL.md for a skill name or a direct file path."""
    as_path = Path(name_or_path)
    if as_path.is_file():
        return as_path
    if as_path.name == "SKILL.md" and as_path.parent.is_dir():
        return as_path if as_path.exists() else None
    candidate = skills_dir / name_or_path / "SKILL.md"
    return candidate if candidate.is_file() else None


def resolve_dependencies(
    skills: list[str],
    *,
    skills_dir: Path = DEFAULT_SKILLS_DIR,
) -> list[str]:
    """Recursively resolve ``requires:`` from SKILL.md frontmatter.

    Returns dependencies in topological order (deps before dependents),
    preserving the original skill order otherwise.
    Accepts both skill names (looked up in *skills_dir*) and direct file paths.
    """
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
        if name not in resolved:  # pragma: no branch
            resolved.append(name)

    for skill in skills:
        _walk(skill)
    return resolved


def resolve_skill_bundle(
    *,
    phase: str,
    overlay_skill_metadata: SkillMetadata,
    delegation_map_path: Path | None = DEFAULT_SKILL_MAP,
    skills_dir: Path = DEFAULT_SKILLS_DIR,
) -> list[str]:
    skills: list[str] = []
    if skill_path := overlay_skill_metadata.get("skill_path"):
        skills.append(str(skill_path))
    companion_skills = overlay_skill_metadata.get("companion_skills", [])
    if isinstance(companion_skills, list):
        skills.extend(str(companion) for companion in companion_skills)
    skills.extend(load_delegation_map(delegation_map_path).get(phase, []).copy())

    resolved = resolve_dependencies(skills, skills_dir=skills_dir)

    ordered: list[str] = []
    for skill in resolved:
        if skill not in ordered:
            ordered.append(skill)
    return ordered
