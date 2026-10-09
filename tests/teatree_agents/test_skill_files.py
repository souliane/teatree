from pathlib import Path

import pytest

from teatree.agents.skill_files import SkillFileIndex, reach_line


def _skill(root: Path, name: str, *references: str) -> Path:
    skill_dir = root / name
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    for reference in references:
        (skill_dir / "references" / reference).write_text(f"{name}/{reference}\n", encoding="utf-8")
    return skill_dir


class TestRegistration:
    def test_registers_skill_md_and_reference_markdown(self, tmp_path: Path) -> None:
        _skill(tmp_path, "rules", "verification.md")
        index = SkillFileIndex.build([tmp_path])
        assert index.lookup("skills/rules/SKILL.md") == tmp_path / "rules" / "SKILL.md"
        assert index.lookup("skills/rules/references/verification.md") == (
            tmp_path / "rules" / "references" / "verification.md"
        )

    @pytest.mark.parametrize(
        "planted",
        ["references/notes.txt", "references/nested/deep.md", "scripts/tool.md", "README.md"],
    )
    def test_ignores_non_markdown_nested_and_non_reference_files(self, tmp_path: Path, planted: str) -> None:
        skill_dir = _skill(tmp_path, "rules")
        (skill_dir / planted).parent.mkdir(parents=True, exist_ok=True)
        (skill_dir / planted).write_text("x", encoding="utf-8")
        assert SkillFileIndex.build([tmp_path]).lookup(f"skills/rules/{planted}") is None

    def test_ignores_a_directory_without_skill_md(self, tmp_path: Path) -> None:
        (tmp_path / "notaskill" / "references").mkdir(parents=True)
        (tmp_path / "notaskill" / "references" / "x.md").write_text("x", encoding="utf-8")
        assert SkillFileIndex.build([tmp_path]).lookup("skills/notaskill/references/x.md") is None

    def test_a_symlinked_skill_dir_registers_its_skill_md(self, tmp_path: Path) -> None:
        target = _skill(tmp_path / "elsewhere", "ext")
        root = tmp_path / "harness"
        root.mkdir()
        (root / "ext").symlink_to(target, target_is_directory=True)
        assert SkillFileIndex.build([root]).lookup("skills/ext/SKILL.md") == root / "ext" / "SKILL.md"

    def test_the_first_root_wins_a_repo_relative_key(self, tmp_path: Path) -> None:
        _skill(first := tmp_path / "first", "rules")
        _skill(second := tmp_path / "second", "rules")
        index = SkillFileIndex.build([first, second])
        assert index.lookup("skills/rules/SKILL.md") == first / "rules" / "SKILL.md"
        assert index.lookup(str(second / "rules" / "SKILL.md")) == second / "rules" / "SKILL.md"

    def test_a_missing_root_is_skipped(self, tmp_path: Path) -> None:
        _skill(tmp_path, "rules")
        assert SkillFileIndex.build([tmp_path / "absent", tmp_path]).lookup("skills/rules/SKILL.md") is not None


class TestLookupRefuses:
    def test_an_unregistered_absolute_path(self, tmp_path: Path) -> None:
        _skill(tmp_path, "rules")
        (tmp_path / "secret.md").write_text("x", encoding="utf-8")
        assert SkillFileIndex.build([tmp_path]).lookup(str(tmp_path / "secret.md")) is None

    @pytest.mark.parametrize(
        "candidate",
        [
            "skills/rules/references/../SKILL.md",
            "skills/../pyproject.toml",
            "./skills/rules/SKILL.md",
            "skills/rules/SKILL.md.bak",
            "skills/rulesX/SKILL.md",
            "skills/rules",
            "skills",
            "",
        ],
    )
    def test_path_arithmetic_and_look_alikes(self, tmp_path: Path, candidate: str) -> None:
        _skill(tmp_path, "rules")
        (tmp_path / "rulesX").mkdir()
        assert SkillFileIndex.build([tmp_path]).lookup(candidate) is None

    def test_a_dotdot_absolute_path_to_a_registered_file(self, tmp_path: Path) -> None:
        _skill(tmp_path, "rules", "a.md")
        index = SkillFileIndex.build([tmp_path])
        assert index.lookup(f"{tmp_path}/rules/references/../SKILL.md") is None

    def test_the_skills_root_itself(self, tmp_path: Path) -> None:
        _skill(tmp_path, "rules")
        assert SkillFileIndex.build([tmp_path]).lookup(str(tmp_path)) is None

    def test_a_reference_symlinked_out_of_its_skill_folder(self, tmp_path: Path) -> None:
        skill_dir = _skill(tmp_path / "skills", "rules")
        (outside := tmp_path / "outside.md").write_text("secret", encoding="utf-8")
        (skill_dir / "references" / "escape.md").symlink_to(outside)
        index = SkillFileIndex.build([tmp_path / "skills"])
        assert index.lookup("skills/rules/references/escape.md") is None
        assert index.lookup(str(skill_dir / "references" / "escape.md")) is None


def test_reach_line_names_the_skills_dir_and_the_read_tool(tmp_path: Path) -> None:
    line = reach_line(tmp_path)
    assert f"`{tmp_path}/<skill>/" in line
    assert "`skills/<skill>/" in line
    assert "Read" in line
