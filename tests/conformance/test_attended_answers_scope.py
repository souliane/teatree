from pathlib import Path

import pytest

from teatree.agents import skill_injection
from teatree.skill_support.loading import _PHASE_TO_SKILL, SkillLoadingPolicy

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills/interactive/SKILL.md"


@pytest.mark.parametrize(
    "limit",
    [
        "spending money",
        "irreversible deletion of work that exists on no remote",
        "widening a security posture or a credential's use",
        "anything said to a colleague in the owner's name",
    ],
)
def test_attended_answer_policy_keeps_owner_limits(limit: str) -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "Answer the owner's cards in his place (attended session only)" in text
    assert "below Rules 0-5" in text
    assert limit in text


@pytest.mark.parametrize("command", ["questions list", "questions dismiss", "worktree attest"])
def test_attended_answer_commands_exist(command: str) -> None:
    text = SKILL.read_text(encoding="utf-8")
    reference = (ROOT / "docs/generated/cli-reference.md").read_text(encoding="utf-8")
    assert f"t3 <overlay> {command}" in text
    assert f"t3 teatree {command}`" in reference


def test_headless_rules_do_not_answer_owner_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skill_injection, "harness_skills_dirs", lambda: [ROOT / "skills"])
    rendered = skill_injection.read_only_venue_skill_contents(["rules"], {"rules"})
    assert "0. **Nothing may endanger security or leak:**" in rendered
    assert "Rule -1" not in rendered


def test_runtime_phases_do_not_load_the_attended_skill(tmp_path: Path) -> None:
    policy = SkillLoadingPolicy()
    for phase in _PHASE_TO_SKILL:
        bundle = policy.select_for_runtime_phase(cwd=tmp_path, phase=phase, overlay_skill_metadata={})
        assert bundle.lifecycle_skill
        assert bundle.skills
        assert "interactive" not in bundle.skills
