"""Tests for ``t3 tool validate-skill-refs`` — the CLI surface.

Mirrors ``src/teatree/cli/skill_ref_tools.py``. The dangling-reference logic
itself is covered in ``tests/test_skill_ref_validator.py``; these assert the
CLI wiring — exit codes, human output, and the ``--json`` shape.
"""

import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli import app

runner = CliRunner()


def _seed_skills(root: Path, names: list[str]) -> Path:
    for name in names:
        skill_dir = root / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n", encoding="utf-8")
    return root


class TestValidateSkillRefsCommand:
    def test_dangling_supplementary_ref_exits_one(self, tmp_path: Path) -> None:
        skills = _seed_skills(tmp_path / "skills", ["ac-reviewing-codebase", "ac-django"])
        config = tmp_path / ".teatree-skills.yml"
        config.write_text("ac-reviewing-skills: '\\breview\\b'\nac-django: '.'\n", encoding="utf-8")
        agents = tmp_path / "agents"
        agents.mkdir()
        with patch("teatree.skill_support.ref_validator.default_search_dirs", return_value=[skills]):
            result = runner.invoke(
                app, ["tool", "validate-skill-refs", "--config", str(config), "--agents-dir", str(agents)]
            )
        assert result.exit_code == 1
        assert "ac-reviewing-skills" in result.output
        assert "ac-reviewing-codebase" in result.output

    def test_clean_refs_exit_zero(self, tmp_path: Path) -> None:
        skills = _seed_skills(tmp_path / "skills", ["ac-reviewing-codebase", "ac-django"])
        config = tmp_path / ".teatree-skills.yml"
        config.write_text("ac-reviewing-codebase: '\\breview\\b'\nac-django: '.'\n", encoding="utf-8")
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "coder.md").write_text("---\nname: coder\nskills:\n  - ac-django\n---\n# C", encoding="utf-8")
        with patch("teatree.skill_support.ref_validator.default_search_dirs", return_value=[skills]):
            result = runner.invoke(
                app, ["tool", "validate-skill-refs", "--config", str(config), "--agents-dir", str(agents)]
            )
        assert result.exit_code == 0
        assert "PASS" in result.output

    def test_json_output_lists_findings(self, tmp_path: Path) -> None:
        skills = _seed_skills(tmp_path / "skills", ["ac-django"])
        config = tmp_path / ".teatree-skills.yml"
        config.write_text("ac-reviewing-skills: '\\breview\\b'\n", encoding="utf-8")
        agents = tmp_path / "agents"
        agents.mkdir()
        with patch("teatree.skill_support.ref_validator.default_search_dirs", return_value=[skills]):
            result = runner.invoke(
                app,
                ["tool", "validate-skill-refs", "--config", str(config), "--agents-dir", str(agents), "--json"],
            )
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload[0]["name"] == "ac-reviewing-skills"
