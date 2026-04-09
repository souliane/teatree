"""Tests for scripts/check_skill_versions.py — SKILL.md version enforcement."""

from pathlib import Path
from unittest.mock import patch

import check_skill_versions


class TestProjectVersion:
    def test_reads_version(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nversion = "1.2.3"\n')
        with patch.object(check_skill_versions, "PYPROJECT_PATH", pyproject):
            assert check_skill_versions._project_version() == "1.2.3"

    def test_returns_none_when_missing(self, tmp_path: Path) -> None:
        with patch.object(check_skill_versions, "PYPROJECT_PATH", tmp_path / "nope.toml"):
            assert check_skill_versions._project_version() is None

    def test_returns_none_when_no_version_key(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'foo'\n")
        with patch.object(check_skill_versions, "PYPROJECT_PATH", pyproject):
            assert check_skill_versions._project_version() is None


class TestSkillVersion:
    def test_reads_version_from_frontmatter(self, tmp_path: Path) -> None:
        skill = tmp_path / "SKILL.md"
        skill.write_text("---\nname: t3:x\nmetadata:\n  version: 0.1.0\n---\n")
        assert check_skill_versions._skill_version(skill) == "0.1.0"

    def test_returns_none_on_no_frontmatter(self, tmp_path: Path) -> None:
        skill = tmp_path / "SKILL.md"
        skill.write_text("# No frontmatter")
        assert check_skill_versions._skill_version(skill) is None

    def test_returns_none_on_no_version(self, tmp_path: Path) -> None:
        skill = tmp_path / "SKILL.md"
        skill.write_text("---\nname: t3:x\n---\n")
        assert check_skill_versions._skill_version(skill) is None


class TestFixVersion:
    def test_rewrites_version(self, tmp_path: Path) -> None:
        skill = tmp_path / "SKILL.md"
        skill.write_text("---\nname: t3:x\nmetadata:\n  version: 0.0.1\n---\n# Body\n")
        assert check_skill_versions._fix_version(skill, "1.0.0") is True
        assert "  version: 1.0.0" in skill.read_text()

    def test_returns_false_when_no_change(self, tmp_path: Path) -> None:
        skill = tmp_path / "SKILL.md"
        skill.write_text("# No version line here\n")
        assert check_skill_versions._fix_version(skill, "1.0.0") is False


class TestMain:
    def test_returns_zero_when_all_match(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nversion = "0.1.0"\n')
        skill_dir = tmp_path / "skills" / "t3-demo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: t3:demo\nmetadata:\n  version: 0.1.0\n---\n")
        with (
            patch.object(check_skill_versions, "PYPROJECT_PATH", pyproject),
            patch.object(check_skill_versions, "ROOT_DIR", tmp_path),
        ):
            assert check_skill_versions.main() == 0

    def test_fixes_mismatch_and_returns_one(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nversion = "1.0.0"\n')
        skill_dir = tmp_path / "skills" / "t3-bad"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: t3:bad\nmetadata:\n  version: 0.0.1\n---\n")
        with (
            patch.object(check_skill_versions, "PYPROJECT_PATH", pyproject),
            patch.object(check_skill_versions, "ROOT_DIR", tmp_path),
        ):
            assert check_skill_versions.main() == 1
        # Verify the file was actually fixed
        assert "  version: 1.0.0" in (skill_dir / "SKILL.md").read_text()

    def test_reports_unfixable_mismatch(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nversion = "1.0.0"\n')
        skill_dir = tmp_path / "skills" / "t3-nover"
        skill_dir.mkdir(parents=True)
        # No version line at all — _skill_version returns None, _fix_version returns False
        (skill_dir / "SKILL.md").write_text("---\nname: t3:nover\n---\n# Body\n")
        with (
            patch.object(check_skill_versions, "PYPROJECT_PATH", pyproject),
            patch.object(check_skill_versions, "ROOT_DIR", tmp_path),
        ):
            assert check_skill_versions.main() == 1

    def test_returns_one_when_no_pyproject_version(self, tmp_path: Path) -> None:
        with (
            patch.object(check_skill_versions, "PYPROJECT_PATH", tmp_path / "nope.toml"),
            patch.object(check_skill_versions, "ROOT_DIR", tmp_path),
        ):
            assert check_skill_versions.main() == 1
