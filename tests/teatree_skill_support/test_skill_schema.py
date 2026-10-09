"""Tests for teatree.skill_support.schema — SKILL.md frontmatter validation."""

from pathlib import Path

from teatree.skill_support.schema import installed_skill_names, validate_directory, validate_skill_md


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


class TestPluginProvidedRequiresResolve:
    """A ``requires:`` target that ships via the plugin — not apm-installed — resolves.

    The real shape: ``~/.claude/skills/ac-reviewing-codebase`` declares ``requires: review``
    while ``review`` lives only in the plugin's own ``skills/`` tree, so a validator whose
    known set is the apm dir alone calls a correct declaration broken.
    """

    @staticmethod
    def _plugin_tree(root: Path, *names: str) -> Path:
        for name in names:
            skill = root / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n")
        return root

    def test_directory_resolves_plugin_provided_requires(self, tmp_path: Path, monkeypatch):
        plugin = self._plugin_tree(tmp_path / "plugin-skills", "review")
        monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", str(plugin))
        apm = tmp_path / "apm-skills"
        consumer = apm / "ac-reviewing-codebase"
        consumer.mkdir(parents=True)
        (consumer / "SKILL.md").write_text(
            "---\nname: ac-reviewing-codebase\ndescription: d\nrequires:\n  - review\n---\n"
        )

        errors, _ = validate_directory(apm)

        assert errors == []

    def test_single_file_resolves_plugin_provided_companion(self, tmp_path: Path, monkeypatch):
        plugin = self._plugin_tree(tmp_path / "plugin-skills", "ship")
        monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", str(plugin))
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: test\ndescription: d\ncompanions:\n  - ship\n---\n")

        errors, _ = validate_skill_md(skill_md, known_skills=set())

        assert errors == []

    def test_installed_skill_names_enumerates_every_search_dir(self, tmp_path: Path, monkeypatch):
        plugin = self._plugin_tree(tmp_path / "plugin-skills", "review", "ship")
        apm = self._plugin_tree(tmp_path / "apm-skills", "ac-python")
        monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", f"{plugin}:{apm}")

        assert installed_skill_names() == {"review", "ship", "ac-python"}

    def test_a_genuinely_absent_target_still_fails(self, tmp_path: Path, monkeypatch):
        plugin = self._plugin_tree(tmp_path / "plugin-skills", "review")
        monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", str(plugin))
        apm = tmp_path / "apm-skills"
        consumer = apm / "skill-a"
        consumer.mkdir(parents=True)
        (consumer / "SKILL.md").write_text("---\nname: skill-a\ndescription: d\nrequires:\n  - nonexistent\n---\n")

        errors, _ = validate_directory(apm)

        assert any("requires unknown skill 'nonexistent'" in e for e in errors)


class TestInlineListFieldIsRefused:
    """An inline ``requires``/``companions`` loads as NO dependencies, so it must not validate.

    ``requires_parser`` reads only the block sequence: a flow sequence or a bare scalar
    resolves to an empty dependency list at load time, and every reference in it goes
    unchecked here.
    """

    def test_a_flow_sequence_requires_is_an_error(self, tmp_path: Path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: test\ndescription: d\nrequires: [nonexistent]\n---\n")
        errors, _ = validate_skill_md(skill_md, known_skills=set())
        assert any("requires must be a block list" in e for e in errors)

    def test_a_scalar_companions_is_an_error(self, tmp_path: Path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: test\ndescription: d\ncompanions: nonexistent\n---\n")
        errors, _ = validate_skill_md(skill_md, known_skills=set())
        assert any("companions must be a block list" in e for e in errors)

    def test_an_empty_block_opener_stays_valid(self, tmp_path: Path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: test\ndescription: d\nrequires:\ncompanions:\n---\n")
        errors, _ = validate_skill_md(skill_md, known_skills=set())
        assert errors == []
