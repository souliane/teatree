"""Integration tests for ``teatree.core.skill_cache``."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import teatree.core.skill_cache as skill_cache_mod
from teatree.core.skill_cache import _validate_skills, write_skill_metadata_cache
from teatree.skill_support import index as skill_index

_FRONTMATTER = (
    "---\nname: example\ndescription: example skill\nrequires:\n    - rules\n    - workspace\n---\n\n# Body\n"
)

#: ``find_project_root`` redirects a worktree to its main clone; a test of this tree's skills pins them here.
_THIS_CHECKOUT_SKILLS = Path(__file__).resolve().parents[2] / "skills"


def _make_skills_dir(tmp_path: Path, skills: dict[str, str | None]) -> Path:
    """Build a skills-root-shaped directory under tmp_path; ``None`` content means no SKILL.md."""
    root = tmp_path / "skills"
    root.mkdir()
    for name, content in skills.items():
        d = root / name
        d.mkdir()
        if content is not None:
            (d / "SKILL.md").write_text(content, encoding="utf-8")
    return root


def _write(data_dir: Path) -> dict:
    overlay = MagicMock()
    overlay.metadata.get_skill_metadata.return_value = {"skill_path": "skills/foo/SKILL.md"}
    with (
        patch.object(skill_cache_mod, "DATA_DIR", data_dir),
        patch.object(skill_cache_mod, "get_overlay", return_value=overlay),
    ):
        write_skill_metadata_cache()
    return json.loads((data_dir / "skill-metadata.json").read_text(encoding="utf-8"))


class TestTheCacheIndexesEverySkillRoot:
    """#4769: the cache indexed only ``~/.claude/skills``, so teatree's own ``requires`` never reached a session."""

    def test_teatree_skills_are_indexed_with_their_requires(self, tmp_path: Path) -> None:
        with patch.object(skill_index, "DEFAULT_SKILLS_DIR", _THIS_CHECKOUT_SKILLS):
            payload = _write(tmp_path / "data")

        by_skill = {entry["skill"]: entry for entry in payload["skill_index"]}
        assert "workspace" in by_skill["review"]["requires"]
        assert payload["resolved_requires"]["review"][-1] == "review"

    def test_an_install_root_only_skill_is_indexed_and_fingerprinted(self, tmp_path: Path) -> None:
        installed = Path.home() / ".agents" / "skills" / "example" / "SKILL.md"
        installed.parent.mkdir(parents=True)
        installed.write_text(_FRONTMATTER, encoding="utf-8")
        with patch.object(skill_index, "DEFAULT_SKILLS_DIR", tmp_path / "no-repo-skills"):
            payload = _write(tmp_path / "data")

        assert payload["skill_index"] == [{"skill": "example", "requires": ["rules", "workspace"], "companions": []}]
        assert set(payload["skill_mtimes"]) == {str(installed)}


class TestValidateSkills:
    def test_validates_the_winning_skill_md_once_per_name(self, tmp_path: Path) -> None:
        skills_dir = _make_skills_dir(tmp_path, {"example": _FRONTMATTER, "no-skill-md": None})
        with patch.object(skill_cache_mod, "validate_skill_md", return_value=([], [])) as validator:
            _validate_skills([skills_dir], {"example", "no-skill-md"})
        validator.assert_called_once_with(skills_dir / "example" / "SKILL.md", known_skills={"example", "no-skill-md"})

    @pytest.mark.parametrize(("errors", "warnings"), [(["err"], []), ([], ["warn"])])
    def test_logs_errors_and_warnings(self, tmp_path: Path, errors: list[str], warnings: list[str]) -> None:
        skills_dir = _make_skills_dir(tmp_path, {"example": _FRONTMATTER})
        with (
            patch.object(skill_cache_mod, "validate_skill_md", return_value=(errors, warnings)),
            patch.object(skill_cache_mod.logger, "warning") as log_warning,
        ):
            _validate_skills([skills_dir], {"example"})
        assert log_warning.call_count == 1


class TestWriteSkillMetadataCache:
    def test_writes_json_with_skill_index(self, tmp_path: Path) -> None:
        skills_dir = _make_skills_dir(tmp_path, {"example": _FRONTMATTER})
        overlay = MagicMock()
        overlay.metadata.get_skill_metadata.return_value = {"skill_path": "skills/foo/SKILL.md"}
        data_dir = tmp_path / "data"
        with (
            patch.object(skill_index, "DEFAULT_SKILLS_DIR", skills_dir),
            patch.object(skill_cache_mod, "DATA_DIR", data_dir),
            patch.object(skill_cache_mod, "get_overlay", return_value=overlay),
            patch.object(skill_cache_mod, "resolve_all", return_value={}),
        ):
            write_skill_metadata_cache()

        cache_file = data_dir / "skill-metadata.json"
        assert cache_file.is_file()
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        assert payload["skill_path"] == "skills/foo/SKILL.md"
        assert payload["skill_index"][0]["skill"] == "example"
        assert payload["skill_index"][0]["requires"] == ["rules", "workspace"]
        assert "teatree_version" in payload
        assert "skill_mtimes" in payload
