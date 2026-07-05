"""Tests for teatree.skill_support.schema — SKILL.md frontmatter validation."""

from pathlib import Path

from teatree.skill_support.schema import validate_directory, validate_skill_md


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

    def test_companions_field_is_recognised_not_removed(self, tmp_path: Path):
        # companions is a distinct SOFT field again — recognised, no error, no
        # "unknown field" warning (unlike the still-removed triggers/search_hints).
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: test\ndescription: d\ncompanions:\n  - rules\n---\n")
        errors, warnings = validate_skill_md(skill_md, known_skills={"rules"})
        assert errors == []
        assert not any("'companions'" in w for w in warnings)

    def test_companions_unknown_skill_ref_errors(self, tmp_path: Path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: test\ndescription: d\ncompanions:\n  - nonexistent\n---\n")
        errors, _ = validate_skill_md(skill_md, known_skills={"workspace", "rules"})
        assert any("companions unknown skill 'nonexistent'" in e for e in errors)

    def test_companions_external_methodology_ref_ok(self, tmp_path: Path):
        # An external methodology skill (no SKILL.md in-repo) is a valid companion.
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: test\ndescription: d\ncompanions:\n  - writing-plans\n---\n")
        errors, _ = validate_skill_md(skill_md, known_skills={"rules"})
        assert errors == []

    def test_removed_triggers_field_fails_loud(self, tmp_path: Path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: test\ndescription: d\ntriggers:\n  priority: 50\n---\n")
        errors, _warnings = validate_skill_md(skill_md)
        assert any("'triggers'" in e and "removed" in e for e in errors)

    def test_removed_search_hints_field_fails_loud(self, tmp_path: Path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: test\ndescription: d\nsearch_hints:\n  - foo\n---\n")
        errors, _warnings = validate_skill_md(skill_md)
        assert any("'search_hints'" in e and "removed" in e for e in errors)

    def test_eval_exempt_field_is_recognised(self, tmp_path: Path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: test\ndescription: d\neval_exempt: pure-doc, no behaviour\n---\n")
        errors, warnings = validate_skill_md(skill_md)
        assert errors == []
        assert not any("'eval_exempt'" in w for w in warnings)

    def test_eval_exempt_empty_is_error(self, tmp_path: Path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: test\ndescription: d\neval_exempt: ''\n---\n")
        errors, _ = validate_skill_md(skill_md)
        assert any("eval_exempt" in e and "non-empty" in e for e in errors)

    def test_eval_exempt_bare_key_is_error(self, tmp_path: Path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: test\ndescription: d\neval_exempt:\n---\n")
        errors, _ = validate_skill_md(skill_md)
        assert any("eval_exempt" in e and "non-empty" in e for e in errors)

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
