"""Tests for teatree.skill_schema — SKILL.md frontmatter validation."""

from pathlib import Path

from teatree.skill_schema import validate_directory, validate_skill_md


class TestValidateSkillMd:
    def test_valid_minimal(self, tmp_path: Path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: test\ndescription: A test skill\n---\n# Test")
        errors, _warnings = validate_skill_md(skill_md)
        assert errors == []

    def test_missing_name(self, tmp_path: Path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\ndescription: A test skill\n---\n# Test")
        errors, _ = validate_skill_md(skill_md)
        assert any("missing required field 'name'" in e for e in errors)

    def test_missing_description(self, tmp_path: Path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: test\n---\n# Test")
        errors, _ = validate_skill_md(skill_md)
        assert any("missing required field 'description'" in e for e in errors)

    def test_missing_frontmatter(self, tmp_path: Path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("# No frontmatter")
        errors, _ = validate_skill_md(skill_md)
        assert any("missing YAML frontmatter" in e for e in errors)

    def test_unclosed_frontmatter(self, tmp_path: Path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: test\n")
        errors, _ = validate_skill_md(skill_md)
        assert any("unclosed frontmatter" in e for e in errors)

    def test_unknown_fields_produce_warnings(self, tmp_path: Path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: test\ndescription: d\ncustom_field: value\n---\n")
        errors, warnings = validate_skill_md(skill_md)
        assert errors == []
        assert any("unknown field 'custom_field'" in w for w in warnings)

    def test_invalid_regex_in_keywords(self, tmp_path: Path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: test\ndescription: d\ntriggers:\n  keywords:\n    - '[invalid'\n---\n")
        errors, _ = validate_skill_md(skill_md)
        assert any("invalid regex" in e for e in errors)

    def test_valid_regex_in_keywords(self, tmp_path: Path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: test\ndescription: d\ntriggers:\n  keywords:\n    - '\\bcommit\\b'\n---\n")
        errors, _ = validate_skill_md(skill_md)
        assert errors == []

    def test_requires_unknown_skill(self, tmp_path: Path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: test\ndescription: d\nrequires:\n  - nonexistent\n---\n")
        errors, _ = validate_skill_md(skill_md, known_skills={"workspace", "rules"})
        assert any("requires unknown skill 'nonexistent'" in e for e in errors)

    def test_requires_known_skill_ok(self, tmp_path: Path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: test\ndescription: d\nrequires:\n  - workspace\n---\n")
        errors, _ = validate_skill_md(skill_md, known_skills={"workspace"})
        assert errors == []

    def test_file_not_found(self, tmp_path: Path):
        errors, _ = validate_skill_md(tmp_path / "missing.md")
        assert any("file not found" in e for e in errors)

    def test_unreadable_file(self, tmp_path: Path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: test\n---\n")
        skill_md.chmod(0o000)
        errors, _ = validate_skill_md(skill_md)
        assert len(errors) == 1
        skill_md.chmod(0o644)  # Restore for cleanup


class TestValidateDirectory:
    def test_validates_all_skills(self, tmp_path: Path):
        for name in ("skill-a", "skill-b"):
            d = tmp_path / name
            d.mkdir()
            (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n")
        errors, _ = validate_directory(tmp_path)
        assert errors == []

    def test_cross_validates_requires(self, tmp_path: Path):
        a = tmp_path / "skill-a"
        a.mkdir()
        (a / "SKILL.md").write_text("---\nname: skill-a\ndescription: d\nrequires:\n  - skill-b\n---\n")
        b = tmp_path / "skill-b"
        b.mkdir()
        (b / "SKILL.md").write_text("---\nname: skill-b\ndescription: d\n---\n")
        errors, _ = validate_directory(tmp_path)
        assert errors == []

    def test_catches_missing_requires_ref(self, tmp_path: Path):
        a = tmp_path / "skill-a"
        a.mkdir()
        (a / "SKILL.md").write_text("---\nname: skill-a\ndescription: d\nrequires:\n  - nonexistent\n---\n")
        errors, _ = validate_directory(tmp_path)
        assert any("requires unknown skill 'nonexistent'" in e for e in errors)
