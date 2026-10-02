"""The recommended-skill advisory INFO-suggests but never gates (#3668)."""

from pathlib import Path

import typer
from typer.testing import CliRunner

from teatree.cli.doctor.checks_recommendations import _check_recommended_skills
from teatree.provisioning.recommended import RECOMMENDED_SKILLS


def _run(search_dirs: list[Path]) -> tuple[bool, str]:
    holder: dict[str, bool] = {}
    app = typer.Typer()

    @app.command()
    def run() -> None:
        holder["ok"] = _check_recommended_skills(search_dirs=search_dirs)

    result = CliRunner().invoke(app, [])
    return holder["ok"], result.output


def test_absent_recommendation_is_info_and_never_gates(tmp_path: Path) -> None:
    ok, output = _run([tmp_path])
    assert ok is True
    assert "INFO" in output
    assert "claude-api" in output
    assert "Anthropic-specific" in output
    assert "gates nothing" in output
    assert "FAIL" not in output
    assert "WARN" not in output


def test_installed_recommendation_is_silent(tmp_path: Path) -> None:
    for rec in RECOMMENDED_SKILLS:
        (tmp_path / rec.name).mkdir(parents=True)
        (tmp_path / rec.name / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    ok, output = _run([tmp_path])
    assert ok is True
    assert output.strip() == ""


def test_advisory_names_the_install_command(tmp_path: Path) -> None:
    _, output = _run([tmp_path])
    assert "apm install" in output
