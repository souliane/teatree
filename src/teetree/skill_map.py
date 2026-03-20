from collections.abc import Mapping, Sequence
from pathlib import Path

DEFAULT_SKILL_MAP = Path("references/skill-delegation.md")
DEFAULT_SKILL_DELEGATION: dict[str, tuple[str, ...]] = {
    "coding": ("test-driven-development", "verification-before-completion"),
    "debugging": ("systematic-debugging", "verification-before-completion"),
    "reviewing": ("requesting-code-review", "verification-before-completion"),
    "shipping": ("finishing-a-development-branch", "verification-before-completion"),
    "ticket-intake": ("writing-plans",),
}


def default_skill_delegation() -> dict[str, list[str]]:
    return {phase: list(skills) for phase, skills in DEFAULT_SKILL_DELEGATION.items()}


def parse_skill_delegation_map(markdown: str) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    current_phase = ""
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            current_phase = line.removeprefix("## ").strip()
            mapping[current_phase] = []
            continue
        if line.startswith("- ") and current_phase:
            mapping[current_phase].append(line.removeprefix("- ").strip())
    return mapping


def render_skill_delegation_map(mapping: Mapping[str, Sequence[str]]) -> str:
    lines = ["# Skill Delegation", ""]
    for phase, skills in mapping.items():
        lines.extend((f"## {phase}", ""))
        lines.extend(f"- {skill}" for skill in skills)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def load_skill_delegation(path: Path | None = DEFAULT_SKILL_MAP) -> tuple[str, dict[str, list[str]]]:
    if path is None:
        return "teetree.skill_map.DEFAULT_SKILL_DELEGATION", default_skill_delegation()
    if path.exists():
        return str(path), parse_skill_delegation_map(path.read_text(encoding="utf-8"))
    if path.as_posix() == DEFAULT_SKILL_MAP.as_posix():
        return "teetree.skill_map.DEFAULT_SKILL_DELEGATION", default_skill_delegation()
    msg = f"Skill delegation map not found: {path}"
    raise FileNotFoundError(msg)
