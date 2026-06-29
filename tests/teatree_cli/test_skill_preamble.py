"""``t3 <overlay> skill-preamble`` — the orchestrator's sub-agent dispatch preamble.

A raw Agent-tool sub-agent inherits none of the orchestrator's loaded skills, so
the orchestrator prepends this command's output (the concatenated ``SKILL.md``
bodies) to every brief. These tests drive the command through the real overlay
Typer app and assert the emitted preamble carries both framework and overlay
skill bodies, and fails loud when a requested skill cannot be resolved.
"""

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from teatree.cli.overlay import OverlayAppBuilder, _overlay_skills_dir, _split_skill_args


def _write_skill(skills_dir: Path, name: str, body: str) -> None:
    d = skills_dir / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


@pytest.fixture
def overlay_project(tmp_path: Path) -> Path:
    skills = tmp_path / "skills"
    _write_skill(skills, "acme", "# Acme overlay\nUse `t3 acme` for every workspace op, never raw glab.")
    _write_skill(skills, "acme-e2e", "# Acme e2e\nReuse the running dev-env; do not over-provision for remote e2e.")
    return tmp_path


@pytest.fixture
def app(overlay_project: Path) -> typer.Typer:
    return OverlayAppBuilder(overlay_name="acme", project_path=overlay_project).build()


class TestSkillPreambleEmission:
    def test_emits_overlay_skill_body(self, app: typer.Typer) -> None:
        result = CliRunner().invoke(app, ["skill-preamble", "--skills", "acme"])
        assert result.exit_code == 0, result.output
        assert "--- SKILL: acme ---" in result.output
        assert "Use `t3 acme` for every workspace op, never raw glab." in result.output

    def test_embeds_framework_and_overlay_bodies_together(self, app: typer.Typer) -> None:
        result = CliRunner().invoke(app, ["skill-preamble", "--skills", "t3:rules,acme"])
        assert result.exit_code == 0, result.output
        assert "--- SKILL: rules ---" in result.output
        assert "--- SKILL: acme ---" in result.output

    def test_comma_separated_skills_each_resolve(self, app: typer.Typer) -> None:
        result = CliRunner().invoke(app, ["skill-preamble", "--skills", "acme,acme-e2e"])
        assert result.exit_code == 0, result.output
        assert "--- SKILL: acme ---" in result.output
        assert "--- SKILL: acme-e2e ---" in result.output

    def test_repeated_skill_options_each_resolve(self, app: typer.Typer) -> None:
        result = CliRunner().invoke(app, ["skill-preamble", "--skill", "acme", "--skill", "acme-e2e"])
        assert result.exit_code == 0, result.output
        assert "--- SKILL: acme ---" in result.output
        assert "--- SKILL: acme-e2e ---" in result.output

    def test_carries_the_follow_directive(self, app: typer.Typer) -> None:
        result = CliRunner().invoke(app, ["skill-preamble", "--skills", "acme"])
        assert "does not auto-load" in result.output


class TestSkillPreambleWithoutOverlaySkillsDir:
    def test_framework_skill_resolves_when_no_overlay_skills_dir(self) -> None:
        app = OverlayAppBuilder(overlay_name="acme", project_path=None).build()
        result = CliRunner().invoke(app, ["skill-preamble", "--skills", "t3:rules"])
        assert result.exit_code == 0, result.output
        assert "--- SKILL: rules ---" in result.output


class TestSkillPreambleFailsLoud:
    def test_missing_skill_exits_nonzero_and_names_it(self, app: typer.Typer) -> None:
        result = CliRunner().invoke(app, ["skill-preamble", "--skills", "acme,ghost-skill"])
        assert result.exit_code == 1
        assert "ghost-skill" in result.output
        assert "--- SKILL: acme ---" not in result.output

    def test_no_skills_given_exits_nonzero(self, app: typer.Typer) -> None:
        result = CliRunner().invoke(app, ["skill-preamble"])
        assert result.exit_code == 1


class TestSplitSkillArgs:
    def test_flattens_comma_and_repeats_order_preserving(self) -> None:
        assert _split_skill_args(["t3:rules,t3:e2e", "acme"]) == ["t3:rules", "t3:e2e", "acme"]

    def test_drops_blank_segments(self) -> None:
        assert _split_skill_args(["t3:rules, ,", ""]) == ["t3:rules"]


class TestOverlaySkillsDir:
    def test_returns_project_skills_when_present(self, tmp_path: Path) -> None:
        (tmp_path / "skills" / "acme").mkdir(parents=True)
        assert _overlay_skills_dir(tmp_path) == tmp_path / "skills"

    def test_none_when_no_skills_dir(self, tmp_path: Path) -> None:
        assert _overlay_skills_dir(tmp_path) is None

    def test_none_when_no_project_path(self) -> None:
        assert _overlay_skills_dir(None) is None
