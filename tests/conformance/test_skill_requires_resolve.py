"""Every skill ``requires`` edge names a skill that exists: teatree's own, or one ``apm.yml`` pins.

The skill loader passes an unknown name through unchanged so an external skill still loads, so a
``requires`` left pointing at a renamed or merged teatree skill fails only when the agent loads it.
"""

from pathlib import Path

from teatree.provisioning.declared import skills_declared_in_apm_manifest
from teatree.skill_support.requires_parser import parse_requires

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "skills"


def test_every_skill_requires_edge_resolves() -> None:
    teatree_skills = {skill_md.parent.name for skill_md in SKILLS_DIR.glob("*/SKILL.md")}
    pinned_skills = {dependency.name for dependency in skills_declared_in_apm_manifest(REPO_ROOT / "apm.yml")}
    dangling = sorted(
        f"{skill_md.parent.name} -> {required}"
        for skill_md in SKILLS_DIR.glob("*/SKILL.md")
        for required in parse_requires(skill_md.read_text(encoding="utf-8")) or []
        if required.rpartition(":")[2] not in teatree_skills and required not in pinned_skills
    )
    assert teatree_skills, f"no SKILL.md found under {SKILLS_DIR}"
    assert "writing-plans" in pinned_skills, "apm.yml no longer reads as pinning the methodology skills"
    assert not dangling, f"`requires` names a skill with no SKILL.md and no external home: {dangling}"
