"""OPTIONAL recommended skills are offered, never mandated (#3668)."""

from pathlib import Path

from teatree.provisioning.recommended import RECOMMENDED_SKILLS, RecommendedSkill, unprovisioned_recommendations


def _install(search_dir: Path, name: str) -> None:
    (search_dir / name).mkdir(parents=True)
    (search_dir / name / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")


def test_vendor_architecture_skill_is_recommended_not_mandated() -> None:
    names = {rec.name for rec in RECOMMENDED_SKILLS}
    assert "claude-api" in names


def test_vendor_skill_declares_its_provider_caveat_and_a_pinned_source() -> None:
    rec = next(r for r in RECOMMENDED_SKILLS if r.name == "claude-api")
    assert "anthropic" in rec.caveat.lower()
    assert "#" in rec.source  # a pinned ref, not a floating default branch
    assert rec.source.startswith("anthropics/skills/")


def test_install_hint_is_a_runnable_apm_command() -> None:
    rec = next(r for r in RECOMMENDED_SKILLS if r.name == "claude-api")
    assert rec.install_hint == f"apm install {rec.source}"


def test_overlap_note_distinguishes_local_and_vendor_scope() -> None:
    rec = next(r for r in RECOMMENDED_SKILLS if r.name == "claude-api")
    assert "architecture-design" in rec.overlap_note


def test_unprovisioned_lists_absent_recommendations(tmp_path: Path) -> None:
    rec = RecommendedSkill(
        name="vendor-skill",
        source="owner/repo/skills/vendor-skill#deadbee",
        caveat="Anthropic-specific",
        rationale="does things",
        overlap_note="vs local architecture-design",
    )
    assert unprovisioned_recommendations([tmp_path], [rec]) == [rec]


def test_unprovisioned_skips_installed_recommendations(tmp_path: Path) -> None:
    rec = RecommendedSkill(
        name="vendor-skill",
        source="owner/repo/skills/vendor-skill#deadbee",
        caveat="Anthropic-specific",
        rationale="does things",
        overlap_note="vs local architecture-design",
    )
    _install(tmp_path, "vendor-skill")
    assert unprovisioned_recommendations([tmp_path], [rec]) == []
