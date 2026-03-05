"""Tests for scripts/hooks/update_readme_skills.py — README auto-generation."""

from pathlib import Path
from unittest.mock import patch

import update_readme_skills


class TestParseFromtmatter:
    def test_extracts_name_and_description(self, tmp_path: Path) -> None:
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: my-skill\ndescription: Does things\n---\n# Content")
        result = update_readme_skills._parse_frontmatter(skill_md)
        assert result["name"] == "my-skill"
        assert result["description"] == "Does things"

    def test_strips_quotes(self, tmp_path: Path) -> None:
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text('---\nname: "quoted-skill"\n---\n')
        result = update_readme_skills._parse_frontmatter(skill_md)
        assert result["name"] == "quoted-skill"

    def test_skips_lines_without_colon(self, tmp_path: Path) -> None:
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\nname: my-skill\nno-colon-here\n---\n")
        result = update_readme_skills._parse_frontmatter(skill_md)
        assert result == {"name": "my-skill"}

    def test_returns_empty_on_no_frontmatter(self, tmp_path: Path) -> None:
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("# No frontmatter here")
        assert update_readme_skills._parse_frontmatter(skill_md) == {}


class TestBuildTable:
    def test_builds_table_from_skills(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "t3-test"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: t3-test\ndescription: Testing skill. Use when testing.\n---\n")
        with patch.object(update_readme_skills, "ROOT_DIR", tmp_path):
            table = update_readme_skills._build_table()
        assert "| `t3-test` | Testing skill |" in table
        assert "| Skill | Phase |" in table

    def test_truncates_at_use_when(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "t3-code"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: t3-code\ndescription: Write code with TDD. Use when user says implement.\n---\n"
        )
        with patch.object(update_readme_skills, "ROOT_DIR", tmp_path):
            table = update_readme_skills._build_table()
        assert "Write code with TDD" in table
        assert "Use when" not in table

    def test_empty_when_no_skills(self, tmp_path: Path) -> None:
        with patch.object(update_readme_skills, "ROOT_DIR", tmp_path):
            table = update_readme_skills._build_table()
        assert "| Skill | Phase |" in table


class TestMain:
    def test_missing_readme(self, tmp_path: Path) -> None:
        with patch.object(update_readme_skills, "README_PATH", tmp_path / "MISSING.md"):
            assert update_readme_skills.main() == 1

    def test_missing_markers(self, tmp_path: Path) -> None:
        readme = tmp_path / "README.md"
        readme.write_text("# No markers")
        with patch.object(update_readme_skills, "README_PATH", readme):
            assert update_readme_skills.main() == 1

    def test_updates_readme(self, tmp_path: Path) -> None:
        readme = tmp_path / "README.md"
        readme.write_text("Before\n<!-- BEGIN SKILLS -->\nold\n<!-- END SKILLS -->\nAfter")
        skill_dir = tmp_path / "t3-demo"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: t3-demo\ndescription: Demo skill.\n---\n")
        with (
            patch.object(update_readme_skills, "README_PATH", readme),
            patch.object(update_readme_skills, "ROOT_DIR", tmp_path),
        ):
            result = update_readme_skills.main()
        assert result == 1  # file was modified
        content = readme.read_text()
        assert "| `t3-demo` |" in content
        assert "Before" in content
        assert "After" in content

    def test_no_change_returns_zero(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "t3-demo"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: t3-demo\ndescription: Demo.\n---\n")
        # Build the expected content first
        with patch.object(update_readme_skills, "ROOT_DIR", tmp_path):
            table = update_readme_skills._build_table()
        readme = tmp_path / "README.md"
        readme.write_text(f"<!-- BEGIN SKILLS -->\n{table}\n<!-- END SKILLS -->")
        with (
            patch.object(update_readme_skills, "README_PATH", readme),
            patch.object(update_readme_skills, "ROOT_DIR", tmp_path),
        ):
            assert update_readme_skills.main() == 0
