"""Every skill ``requires`` edge names a skill that exists: teatree's own, or an external methodology skill.

The skill loader passes an unknown name through unchanged so an external skill still loads, so a
``requires`` left pointing at a renamed or merged teatree skill fails only when the agent loads it.
"""

from pathlib import Path

from teatree.skill_support.requires_parser import parse_requires

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"

_EXTERNAL_METHODOLOGY_SKILLS = frozenset(
    {
        "finishing-a-development-branch",
        "receiving-code-review",
        "requesting-code-review",
        "systematic-debugging",
        "test-driven-development",
        "verification-before-completion",
        "writing-plans",
    }
)


def test_every_skill_requires_edge_resolves() -> None:
    teatree_skills = {skill_md.parent.name for skill_md in SKILLS_DIR.glob("*/SKILL.md")}
    dangling = sorted(
        f"{skill_md.parent.name} -> {required}"
        for skill_md in SKILLS_DIR.glob("*/SKILL.md")
        for required in parse_requires(skill_md.read_text(encoding="utf-8")) or []
        if required.rpartition(":")[2] not in teatree_skills and required not in _EXTERNAL_METHODOLOGY_SKILLS
    )
    assert teatree_skills, f"no SKILL.md found under {SKILLS_DIR}"
    assert not dangling, f"`requires` names a skill with no SKILL.md and no external home: {dangling}"
